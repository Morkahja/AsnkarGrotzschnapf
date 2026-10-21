import json
import struct
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

from anskar.artwork.settings import GeneratorSettings
from anskar.lab.experiments import LabRequest, list_experiments, resolve_media, run_experiment
from anskar.lab.prompts import compose_prompt, experiment_slug, first_stanza
from anskar.lab.server import make_server
from anskar.style import PRESETS, apply_style, lab_style

POEM = "Little lamb, who made thee?\nDoes thou know who made thee?\n\nLittle lamb, I'll tell thee;"


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, args, *, timeout):
        self.calls.append(list(args))
        output = Path(args[args.index("--output") + 1])
        width = int(args[args.index("--width") + 1])
        height = int(args[args.index("--height") + 1])
        _write_png(output, width, height)
        return subprocess.CompletedProcess(args, 0, b"", b"")


class PromptTests(unittest.TestCase):
    def test_modes_keep_the_typed_text(self):
        title = "THE LAMB"
        self.assertEqual(compose_prompt("title", title, POEM), title)
        self.assertEqual(compose_prompt("body", title, POEM), POEM)
        self.assertEqual(compose_prompt("title_body", title, POEM), f"{title}\n\n{POEM}")

    def test_first_stanza_stops_at_the_blank_line(self):
        self.assertEqual(first_stanza(POEM), "Little lamb, who made thee?\nDoes thou know who made thee?")
        self.assertEqual(first_stanza("\n\nOnly line\n"), "Only line")

    def test_experiment_slug_is_a_folder_name(self):
        self.assertEqual(experiment_slug("Lamb comparison"), "lamb-comparison")
        self.assertEqual(experiment_slug("   "), "ungrouped")
        self.assertEqual(experiment_slug("../secret"), "secret")


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = _settings(self.root)
        self.runner = FakeRunner()
        self.when = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def test_two_seeds_keep_both_images_and_metadata(self):
        first = run_experiment(
            _request(seed=11),
            settings=self.settings,
            runner=self.runner,
            directory=self.root / "experiments",
            now=self.when,
        )
        original = Path(first.output_path).read_bytes()
        second = run_experiment(
            _request(seed=22, body="A different line\nstill exact"),
            settings=self.settings,
            runner=self.runner,
            directory=self.root / "experiments",
            now=self.when,
        )
        self.assertEqual(Path(first.output_path).read_bytes(), original)
        self.assertNotEqual(first.output_path, second.output_path)
        self.assertEqual(self.runner.calls[0][self.runner.calls[0].index("--prompt") + 1], POEM)
        metadata = json.loads(Path(first.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(metadata["body"], POEM)
        self.assertEqual(metadata["title"], "THE LAMB")
        self.assertEqual(metadata["mode"], "body")
        self.assertEqual(metadata["seed"], 11)
        self.assertEqual(metadata["steps"], 8)
        self.assertEqual(metadata["cfg_scale"], 1.0)
        self.assertEqual(metadata["resolution"], "512x512")
        self.assertEqual(metadata["prompt_sha256"], first.prompt_sha256)
        self.assertEqual(metadata["output_path"], first.output_path)
        self.assertIn("model_path", metadata["model_paths"])
        self.assertEqual(metadata["model_id"], "z-image-turbo")
        self.assertEqual(metadata["command_type"], "components")
        self.assertEqual(metadata["negative_prompt"], "")
        self.assertIsNone(metadata["clip_skip"])
        self.assertFalse(metadata["house_style"])
        self.assertEqual(metadata["style_preset"], "none")
        self.assertEqual(metadata["style_prompt"], "")
        self.assertEqual(metadata["style_composition"], "text")
        self.assertEqual(metadata["prompt"], POEM)
        self.assertFalse(self.runner.calls[0][0].endswith("sh"))
        saved = list_experiments(self.root / "experiments")
        self.assertEqual(len(saved), 2)

    def test_print_resolution_is_sent_unchanged(self):
        result = run_experiment(
            LabRequest(
                title="Print master",
                body="A quiet meadow",
                mode="body",
                experiment="print-1600",
                seed=1600,
                width=1600,
                height=1600,
                steps=8,
                cfg_scale=1.0,
            ),
            settings=self.settings,
            runner=self.runner,
            directory=self.root / "experiments",
            now=self.when,
        )
        sent = self.runner.calls[-1]
        self.assertEqual(sent[sent.index("--width") + 1], "1600")
        self.assertEqual(sent[sent.index("--height") + 1], "1600")
        self.assertIn("--vae-tiling", sent)
        metadata = json.loads(Path(result.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(metadata["intended_print"], {"width_inches": 8, "height_inches": 8, "ppi": 200})
        self.assertTrue(metadata["vae_tiling"])
        with self.assertRaises(ValueError):
            run_experiment(
                LabRequest(
                    title="Too large",
                    body="A quiet meadow",
                    mode="body",
                    experiment="print-1600",
                    seed=1,
                    width=2416,
                    height=2416,
                    steps=8,
                    cfg_scale=1.0,
                ),
                settings=self.settings,
                runner=self.runner,
                directory=self.root / "experiments",
                now=self.when,
            )

    def test_existing_file_is_not_overwritten(self):
        folder = self.root / "experiments" / "lamb-comparison"
        folder.mkdir(parents=True)
        existing = folder / "20261021T010000000000Z-body-seed-11.png"
        existing.write_bytes(b"keep")
        result = run_experiment(
            _request(seed=11),
            settings=self.settings,
            runner=self.runner,
            directory=self.root / "experiments",
            now=self.when,
        )
        self.assertEqual(existing.read_bytes(), b"keep")
        self.assertTrue(result.output_path.endswith("-2.png"))

    def test_media_path_stays_inside_the_experiment_folder(self):
        image = self.root / "ok.png"
        _write_png(image, 8, 8)
        self.assertEqual(resolve_media(self.root, "ok.png"), image.resolve())
        with self.assertRaises(ValueError):
            resolve_media(self.root, "../ok.png")

    def test_house_style_is_stored_beside_the_image(self):
        request = LabRequest(
            title="THE LAMB",
            body=POEM,
            mode="body",
            experiment="style",
            seed=4,
            width=512,
            height=512,
            steps=8,
            cfg_scale=1.0,
            house_style=True,
            style_preset="painterly",
            style_prompt=PRESETS["painterly"],
            style_composition="wrapped",
        )
        result = run_experiment(
            request,
            settings=self.settings,
            runner=self.runner,
            directory=self.root / "experiments",
            now=self.when,
        )
        expected = apply_style(POEM, lab_style(
            enabled=True,
            preset="painterly",
            prompt=PRESETS["painterly"],
            composition="wrapped",
        ))
        sent = self.runner.calls[0]
        self.assertEqual(sent[sent.index("--prompt") + 1], expected)
        self.assertEqual(result.prompt, expected)
        self.assertTrue(result.house_style)
        metadata = json.loads(Path(result.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(metadata["body"], POEM)
        self.assertTrue(metadata["house_style"])
        self.assertEqual(metadata["style_preset"], "painterly")
        self.assertEqual(metadata["style_prompt"], PRESETS["painterly"])
        self.assertEqual(metadata["style_composition"], "wrapped")
        self.assertEqual(metadata["prompt"], expected)

    def test_older_experiment_files_are_not_rewritten(self):
        folder = self.root / "experiments" / "older"
        folder.mkdir(parents=True)
        _write_png(folder / "old.png", 64, 64)
        metadata = folder / "old.json"
        original = json.dumps(
            {
                "title": "Cup",
                "body": "Steam",
                "prompt": "Steam",
                "mode": "body",
                "seed": 1,
                "created_at": "2026-10-20T00:00:00+00:00",
                "experiment": "older",
            }
        )
        metadata.write_text(original, encoding="utf-8")
        listed = list_experiments(self.root / "experiments")
        self.assertEqual(listed[0]["prompt"], "Steam")
        self.assertNotIn("house_style", listed[0])
        self.assertEqual(metadata.read_text(encoding="utf-8"), original)

    def test_each_model_builds_its_own_command(self):
        shared = dict(
            title="Boat",
            body="A paper boat.",
            mode="body",
            experiment="models",
            seed=7,
            width=512,
            height=512,
        )
        turbo = run_experiment(
            LabRequest(**shared, steps=8, cfg_scale=1.0),
            settings=self.settings,
            runner=self.runner,
            directory=self.root / "experiments",
            now=self.when,
        )
        turbo_command = self.runner.calls[0]
        self.assertIn("--diffusion-model", turbo_command)
        self.assertNotIn("--model", turbo_command)
        self.assertNotIn("--negative-prompt", turbo_command)
        self.assertEqual(turbo.command_type, "components")
        self.assertEqual(turbo.model_name, "Z-Image Turbo")

        zimage = run_experiment(
            LabRequest(
                **shared,
                model_id="z-image",
                steps=35,
                cfg_scale=4.0,
                negative_enabled=True,
                negative_prompt="text, letters",
            ),
            settings=self.settings,
            runner=self.runner,
            directory=self.root / "experiments",
            now=self.when,
        )
        zimage_command = self.runner.calls[1]
        self.assertTrue(
            zimage_command[zimage_command.index("--diffusion-model") + 1].endswith("z-image-Q4_K_M.gguf")
        )
        self.assertIn("--llm", zimage_command)
        self.assertEqual(zimage_command[zimage_command.index("--negative-prompt") + 1], "text, letters")
        self.assertNotIn("--clip-skip", zimage_command)
        self.assertEqual(zimage.model_id, "z-image")
        self.assertEqual(zimage.negative_prompt, "text, letters")

        realvis = run_experiment(
            LabRequest(
                **{key: value for key, value in shared.items() if key not in {"width", "height"}},
                model_id="realvisxl-v5",
                width=1024,
                height=1024,
                steps=30,
                cfg_scale=5,
                sampler="dpm++2m",
                scheduler="karras",
                clip_skip=2,
                clip_skip_set=True,
                negative_enabled=True,
                negative_prompt="",
            ),
            settings=self.settings,
            runner=self.runner,
            directory=self.root / "experiments",
            now=self.when,
        )
        realvis_command = self.runner.calls[2]
        self.assertEqual(realvis_command[1], "--model")
        self.assertTrue(realvis_command[2].endswith("RealVisXL_V5.0_fp16.safetensors"))
        self.assertNotIn("--diffusion-model", realvis_command)
        self.assertNotIn("--llm", realvis_command)
        self.assertEqual(realvis_command[realvis_command.index("--scheduler") + 1], "karras")
        self.assertEqual(realvis_command[realvis_command.index("--clip-skip") + 1], "2")
        self.assertNotIn("--negative-prompt", realvis_command)
        saved = json.loads(Path(realvis.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["command_type"], "checkpoint")
        self.assertEqual(saved["sampler"], "dpm++2m")
        self.assertEqual(saved["clip_skip"], 2)
        self.assertEqual(saved["negative_prompt"], "")
        self.assertEqual(saved["resolution"], "1024x1024")

        from anskar.models import DREAMSHAPER_NEGATIVE_PROMPT, DREAMSHAPER_PATH

        if not DREAMSHAPER_PATH.is_file():
            self.skipTest("DreamShaper XL is not installed")
        dream = run_experiment(
            LabRequest(
                **{key: value for key, value in shared.items() if key not in {"width", "height"}},
                model_id="dreamshaper-xl",
                width=1024,
                height=1024,
                steps=30,
                cfg_scale=5.5,
                sampler="euler_a",
                negative_enabled=True,
                negative_prompt=DREAMSHAPER_NEGATIVE_PROMPT,
            ),
            settings=self.settings,
            runner=self.runner,
            directory=self.root / "experiments",
            now=self.when,
        )
        dream_command = self.runner.calls[3]
        self.assertEqual(dream_command[1], "--model")
        self.assertTrue(dream_command[2].endswith("DreamShaperXL1.0Alpha2_fixedVae_half_00001_.safetensors"))
        self.assertNotIn("--diffusion-model", dream_command)
        self.assertIn("--offload-to-cpu", dream_command)
        self.assertIn("--vae-tiling", dream_command)
        self.assertEqual(dream_command[dream_command.index("--sampling-method") + 1], "euler_a")
        self.assertEqual(dream_command[dream_command.index("--cfg-scale") + 1], "5.5")
        self.assertEqual(
            dream_command[dream_command.index("--negative-prompt") + 1],
            DREAMSHAPER_NEGATIVE_PROMPT,
        )
        dream_saved = json.loads(Path(dream.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(dream_saved["model_id"], "dreamshaper-xl")
        self.assertEqual(dream_saved["commercial_use"], "unverified")
        self.assertTrue(dream_saved["vae_tiling"])
        self.assertTrue(dream_saved["offload_to_cpu"])
        self.assertEqual(dream_saved["sampler"], "euler_a")
        self.assertIn("model_checksum", dream_saved)
        self.assertEqual(dream_saved["negative_prompt"], DREAMSHAPER_NEGATIVE_PROMPT)

    def test_interpreted_prompt_replaces_the_source_text(self):
        visual = "A lamb of warm light stands in deep grass, with no writing anywhere."
        result = run_experiment(
            LabRequest(
                title="THE LAMB",
                body=POEM,
                mode="body",
                experiment="interpreted",
                seed=9,
                width=512,
                height=512,
                steps=4,
                cfg_scale=1,
                house_style=True,
                style_preset="painterly",
                style_prompt=PRESETS["painterly"],
                style_composition="wrapped",
                method="interpreted",
                visual_prompt=visual + " edited",
                interpretation=visual,
                source_text=POEM,
                ollama={"model": "qwen3:8b", "temperature": 0.7, "num_ctx": 4096, "created_at": "2026-10-21T00:00:00Z"},
                negative_enabled=True,
                negative_prompt="text, letters",
            ),
            settings=self.settings,
            runner=self.runner,
            directory=self.root / "experiments",
            now=self.when,
        )
        command = self.runner.calls[-1]
        sent = command[command.index("--prompt") + 1]
        self.assertEqual(sent, visual + " edited")
        self.assertNotIn("Little lamb, who made thee?", sent)
        self.assertNotIn(PRESETS["painterly"], sent)
        self.assertEqual(command[command.index("--negative-prompt") + 1], "text, letters")
        saved = json.loads(Path(result.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["method"], "interpreted")
        self.assertEqual(saved["source_text"], POEM)
        self.assertEqual(saved["interpretation"], visual)
        self.assertEqual(saved["visual_prompt"], visual + " edited")
        self.assertTrue(saved["interpretation_edited"])
        self.assertEqual(saved["ollama"]["model"], "qwen3:8b")
        self.assertEqual(saved["style_preset"], "painterly")
        self.assertIn("source_sha256", saved)
        self.assertIn("interpretation_sha256", saved)
        self.assertNotEqual(saved["prompt_sha256"], saved["source_sha256"])
        self.assertTrue(result.output_path.endswith("interpreted-body-seed-9.png"))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.server = make_server(
            "127.0.0.1",
            0,
            settings=_settings(root),
            runner=FakeRunner(),
            experiments=root / "experiments",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def test_page_and_generation_round_trip(self):
        page = urllib.request.urlopen(self.base + "/").read().decode("utf-8")
        self.assertIn("Prompt lab", page)
        self.assertIn("Generate", page)
        self.assertIn("Advanced", page)
        self.assertIn("Surreal Abstract", page)
        self.assertIn("Approve for print", page)
        self.assertIn("Asking Ollama", page)
        self.assertNotIn("Use house style", page)
        self.assertNotIn("Wrapped instruction", page)
        self.assertNotIn("Interpret with Ollama", page)
        defaults = json.load(urllib.request.urlopen(self.base + "/api/defaults"))
        self.assertEqual(defaults["style_preset"], "painterly")
        self.assertEqual(defaults["presets"]["painterly"], PRESETS["painterly"])
        preview = _post(self.base + "/api/preview", {"mode": "title_body", "title": "THE LAMB", "body": POEM})
        self.assertEqual(preview["prompt"], f"THE LAMB\n\n{POEM}")
        styled = _post(
            self.base + "/api/preview",
            {
                "mode": "body",
                "title": "THE LAMB",
                "body": POEM,
                "house_style": True,
                "style_preset": "dreamlike",
                "style_prompt": PRESETS["dreamlike"],
                "style_composition": "style_text",
            },
        )
        self.assertEqual(
            styled["prompt"],
            apply_style(
                POEM,
                lab_style(
                    enabled=True,
                    preset="dreamlike",
                    prompt=PRESETS["dreamlike"],
                    composition="style_text",
                ),
            ),
        )
        self.assertEqual(styled["style_preset"], "dreamlike")
        self.assertTrue(styled["house_style"])
        stanza = _post(self.base + "/api/first-stanza", {"body": POEM})
        self.assertEqual(stanza["stanza"], "Little lamb, who made thee?\nDoes thou know who made thee?")
        created = _post(self.base + "/api/generate", _payload(seed=5))
        self.assertEqual(created["seed"], 5)
        image = urllib.request.urlopen(self.base + created["image_url"]).read()
        self.assertTrue(image.startswith(b"\x89PNG"))
        again = _post(self.base + "/api/generate", _payload(seed=6, body="Another exact line"))
        self.assertNotEqual(created["output_path"], again["output_path"])
        listing = json.load(urllib.request.urlopen(self.base + "/api/experiments"))
        self.assertEqual(len(listing["experiments"]), 2)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/media/../../etc/passwd")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()
        with self.assertRaises(ValueError):
            make_server("0.0.0.0", 0)


def _request(seed: int, body: str = POEM) -> LabRequest:
    return LabRequest(
        title="THE LAMB",
        body=body,
        mode="body",
        experiment="Lamb comparison",
        seed=seed,
        width=512,
        height=512,
        steps=8,
        cfg_scale=1.0,
    )


def _payload(seed: int, body: str = POEM) -> dict:
    return {
        "title": "THE LAMB",
        "body": body,
        "mode": "body",
        "experiment": "Lamb comparison",
        "seed": seed,
        "width": 512,
        "height": 512,
        "steps": 8,
        "cfg_scale": 1,
    }


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def _settings(root: Path) -> GeneratorSettings:
    executable = root / "sd-cli"
    diffusion = root / "diffusion.gguf"
    encoder = root / "encoder.gguf"
    vae = root / "ae.safetensors"
    for path in (executable, diffusion, encoder, vae):
        path.write_bytes(b"model")
    executable.chmod(0o755)
    return GeneratorSettings(
        executable=executable,
        diffusion_model=diffusion,
        text_encoder=encoder,
        vae=vae,
        backend="vulkan0",
        steps=8,
        sampling_method="euler",
        cfg_scale=1.0,
        width=512,
        height=512,
        offload_to_cpu=True,
        flash_attn="off,diffusion=on",
        timeout_seconds=30,
        artworks_dir=root / "artworks",
    )


def _write_png(path: Path, width: int, height: int) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr))


if __name__ == "__main__":
    unittest.main()
