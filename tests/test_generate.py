import json
import os
import struct
import subprocess
import tempfile
import unittest
import zlib
from dataclasses import replace
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from anskar.archive.importer import import_collection
from anskar.archive.selection import select_poem
from anskar.artwork.generator import (
    DuplicateArtworkError,
    GenerationError,
    SubprocessRunner,
    build_command,
    generate_pending,
    prompt_hash,
)
from anskar.artwork.png import png_size
from anskar.artwork.settings import GeneratorSettings, load_settings
from anskar.cli import main
from anskar.models import ZIMAGE_PATH
from anskar.style import PRESETS, production_style
from anskar.db import connect

ON = date(2026, 10, 20)
POEM = "Little lamb, who made thee?\n    Does thou know who made thee?"
VISUAL = "An impossible gold vessel floats in a violet room, away from the center."


class FakeInterpreter:
    def __init__(self):
        self.calls = []
        self.released = 0

    def interpret(self, source, choice, abstraction="surreal"):
        self.calls.append((source, choice.preset, abstraction))
        return _Visual(source, abstraction)

    def release_for_image_generation(self):
        self.released += 1


class _Visual:
    def __init__(self, source, abstraction):
        self.prompt = VISUAL
        self.source_text = source
        self.abstraction = abstraction

    def to_record(self):
        return {
            "source_text": self.source_text,
            "interpretation": self.prompt,
            "abstraction": self.abstraction,
            "ollama": {"model": "qwen3:8b", "think": False},
        }


class FirstPoem:
    def choice(self, sequence):
        return sequence[0]


class FakeRunner:
    def __init__(self, *, returncode=0, stderr=b"", size=(512, 512), error=None, write=True):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr
        self.size = size
        self.error = error
        self.write = write

    def run(self, args, *, timeout):
        self.calls.append((list(args), timeout))
        if self.error is not None:
            raise self.error
        if self.write and self.returncode == 0:
            output = Path(args[args.index("--output") + 1])
            _write_png(output, *self.size)
        return subprocess.CompletedProcess(args, self.returncode, b"", self.stderr)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = root / "archive.sqlite"
        self.artworks = root / "artworks"
        self.settings = _settings(root, self.artworks)
        collection = root / "collection.json"
        collection.write_text(
            json.dumps(
                {
                    "collection_id": "generate-test",
                    "poems": [
                        {
                            "title": "THE LAMB",
                            "author": "William Blake",
                            "author_death_year": 1827,
                            "author_death_year_source": "https://example.test/blake",
                            "origin": None,
                            "origin_source": None,
                            "original_language": "en",
                            "text_language": "en",
                            "translator": None,
                            "body": POEM,
                            "source_url": "https://example.test/lamb",
                            "license_name": "Swedish economic rights expired",
                            "license_info": "Documented from SFS 1960:729 section 43 and the cited death year.",
                            "rights_verification_status": "documented",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with connect(self.database) as connection:
            import_collection(connection, collection, on=ON)
            select_poem(connection, chooser=FirstPoem(), selected_at="2026-10-20T00:00:00+00:00")
        self.interpreter = FakeInterpreter()

    def tearDown(self):
        self.temporary.cleanup()

    def test_command_uses_the_poem_unchanged_without_a_shell(self):
        output = self.artworks / "out.png"
        command = build_command(self.settings, POEM, 17, output)
        self.assertEqual(command[command.index("--prompt") + 1], POEM)
        self.assertIn("\n", command[command.index("--prompt") + 1])
        self.assertIn("--offload-to-cpu", command)
        self.assertEqual(command[command.index("--flash-attn") + 1], "off,diffusion=on")
        self.assertEqual(command[command.index("--backend") + 1], "vulkan0")
        self.assertEqual(command[command.index("--sampling-method") + 1], "euler")
        self.assertEqual(command[command.index("--cfg-scale") + 1], "1.0")
        with patch("anskar.artwork.generator.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(command, 0, b"", b"")
            SubprocessRunner().run(command, timeout=12)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index("--prompt") + 1], POEM)
        self.assertEqual(run.call_args.kwargs["timeout"], 12)

    def test_generate_records_one_artwork_and_leaves_the_poem_pending(self):
        runner = FakeRunner()
        record = generate_pending(
            self.database,
            self.settings,
            runner=runner,
            seed=17,
            interpreter=self.interpreter,
        )
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.title, "THE LAMB")
        self.assertEqual(record.author, "William Blake")
        self.assertEqual(record.poem_id, 1)
        self.assertEqual(record.seed, 17)
        self.assertEqual((record.width, record.height), (512, 512))
        self.assertEqual(record.prompt_sha256, prompt_hash(VISUAL))
        self.assertEqual(record.model, str(self.settings.diffusion_model))
        self.assertEqual(png_size(Path(record.output_path)), (512, 512))
        settings = json.loads(record.settings_json)
        self.assertEqual(settings["backend"], "vulkan0")
        self.assertEqual(settings["steps"], 8)
        self.assertTrue(settings["offload_to_cpu"])
        self.assertTrue(settings["house_style"])
        self.assertEqual(settings["model_id"], "z-image-turbo")
        self.assertEqual(settings["command_type"], "components")
        self.assertEqual(settings["style_preset"], "painterly")
        self.assertEqual(settings["style_prompt"], PRESETS["painterly"])
        self.assertEqual(settings["style_composition"], "wrapped")
        self.assertEqual(settings["prompt"], VISUAL)
        self.assertEqual(settings["source_text"], POEM)
        self.assertEqual(settings["method"], "interpreted")
        self.assertEqual(settings["abstraction"], "surreal")
        self.assertEqual(settings["visual_prompt"], VISUAL)
        self.assertFalse(settings["print_suitable"])
        self.assertEqual(settings["ollama"]["model"], "qwen3:8b")
        self.assertNotIn("--negative-prompt", runner.calls[0][0])
        sent = runner.calls[0][0]
        self.assertEqual(sent[sent.index("--prompt") + 1], VISUAL)
        self.assertNotIn(POEM, sent[sent.index("--prompt") + 1])
        self.assertEqual(self.interpreter.calls[0][0], POEM)
        self.assertEqual(self.interpreter.released, 1)
        sidecar = json.loads(Path(record.output_path).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["source_text"], POEM)
        self.assertEqual(sidecar["prompt"], VISUAL)
        self.assertEqual(sidecar["method"], "interpreted")
        with connect(self.database) as connection:
            pending = connection.execute(
                "SELECT status, confirmed_at FROM selection_history"
            ).fetchone()
            stored = connection.execute("SELECT body FROM poems WHERE id = 1").fetchone()
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["confirmed_at"])
        self.assertEqual(stored["body"], POEM)

        with self.assertRaises(DuplicateArtworkError):
            generate_pending(
                self.database,
                self.settings,
                runner=FakeRunner(),
                seed=18,
                interpreter=self.interpreter,
            )
        self.assertEqual(len(list(self.artworks.glob("*.png"))), 1)

    def test_failed_generation_can_be_retried_and_timeout_is_recorded(self):
        failed = FakeRunner(returncode=7, stderr=b"vulkan device lost")
        with self.assertRaises(GenerationError):
            generate_pending(self.database, self.settings, runner=failed, seed=3, interpreter=self.interpreter)
        timeout = FakeRunner(error=subprocess.TimeoutExpired(cmd=["sd-cli"], timeout=5, stderr=b"still loading"))
        with self.assertRaises(GenerationError) as caught:
            generate_pending(self.database, self.settings, runner=timeout, seed=4, interpreter=self.interpreter)
        self.assertIn("timed out", str(caught.exception))
        record = generate_pending(self.database, self.settings, runner=FakeRunner(), seed=5, interpreter=self.interpreter)
        self.assertEqual(record.status, "succeeded")
        with connect(self.database) as connection:
            statuses = [
                row["status"]
                for row in connection.execute("SELECT status FROM artworks ORDER BY id")
            ]
            pending = connection.execute(
                "SELECT status FROM selection_history"
            ).fetchone()["status"]
        self.assertEqual(statuses, ["failed", "failed", "succeeded"])
        self.assertEqual(pending, "pending")

    def test_existing_png_is_not_overwritten(self):
        target = self.artworks / "poem-1-selection-1-seed-9.png"
        target.parent.mkdir()
        target.write_bytes(b"keep")
        with self.assertRaises(GenerationError):
            generate_pending(self.database, self.settings, runner=FakeRunner(), seed=9, interpreter=self.interpreter)
        self.assertEqual(target.read_bytes(), b"keep")
        with connect(self.database) as connection:
            count = connection.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
        self.assertEqual(count, 0)

    def test_wrong_dimensions_and_missing_executable_are_errors(self):
        with self.assertRaises(GenerationError):
            generate_pending(
                self.database,
                self.settings,
                runner=FakeRunner(size=(256, 256)),
                seed=6,
                interpreter=self.interpreter,
            )
        missing = GeneratorSettings(**{**self.settings.__dict__, "executable": Path("/missing/sd-cli")})
        with self.assertRaises(GenerationError):
            generate_pending(self.database, missing, runner=FakeRunner(), seed=7)

    def test_stale_running_generation_does_not_block_a_new_one(self):
        with connect(self.database) as connection:
            connection.execute(
                """
                INSERT INTO artworks (
                    poem_id, selection_id, title, author, prompt_sha256, model, seed,
                    width, height, settings_json, output_path, status, created_at
                ) VALUES (1, 1, 'THE LAMB', 'William Blake', ?, 'model', 1,
                          512, 512, '{}', '/tmp/old.png', 'running', '2020-01-01T00:00:00+00:00')
                """,
                ("a" * 64,),
            )
        record = generate_pending(
            self.database,
            self.settings,
            runner=FakeRunner(),
            seed=11,
            interpreter=self.interpreter,
        )
        self.assertEqual(record.status, "succeeded")
        with connect(self.database) as connection:
            statuses = [
                row["status"]
                for row in connection.execute("SELECT status FROM artworks ORDER BY id")
            ]
        self.assertEqual(statuses, ["failed", "succeeded"])

    def test_style_none_sends_the_stored_poem(self):
        runner = FakeRunner()
        record = generate_pending(
            self.database,
            self.settings,
            runner=runner,
            seed=21,
            style=production_style("none"),
        )
        sent = runner.calls[0][0]
        self.assertEqual(sent[sent.index("--prompt") + 1], POEM)
        saved = json.loads(record.settings_json)
        self.assertFalse(saved["house_style"])
        self.assertEqual(saved["style_preset"], "none")
        self.assertEqual(saved["style_prompt"], "")
        self.assertEqual(saved["prompt"], POEM)
        self.assertEqual(saved["style_composition"], "text")

    def test_text_composition_keeps_the_poem_and_records_the_preset(self):
        runner = FakeRunner()
        record = generate_pending(
            self.database,
            self.settings,
            runner=runner,
            seed=22,
            style=production_style("dreamlike", "text"),
        )
        sent = runner.calls[0][0]
        self.assertEqual(sent[sent.index("--prompt") + 1], POEM)
        saved = json.loads(record.settings_json)
        self.assertTrue(saved["house_style"])
        self.assertEqual(saved["style_preset"], "dreamlike")
        self.assertEqual(saved["style_prompt"], PRESETS["dreamlike"])
        self.assertEqual(saved["style_composition"], "text")
        self.assertEqual(saved["prompt"], POEM)

    def test_dry_run_prints_the_poem_and_does_not_generate(self):
        previous = dict(os.environ)
        os.environ["ANSKAR_DB"] = str(self.database)
        os.environ["ANSKAR_SD_CLI"] = str(self.settings.executable)
        os.environ["ANSKAR_SD_DIFFUSION_MODEL"] = str(self.settings.diffusion_model)
        os.environ["ANSKAR_SD_TEXT_ENCODER"] = str(self.settings.text_encoder)
        os.environ["ANSKAR_SD_VAE"] = str(self.settings.vae)
        os.environ["ANSKAR_ARTWORKS_DIR"] = str(self.artworks)
        os.environ.pop("ANSKAR_SD_WIDTH", None)
        os.environ.pop("ANSKAR_SD_HEIGHT", None)
        try:
            fake = FakeInterpreter()
            with patch("anskar.artwork.generator._default_interpreter", return_value=fake):
                output = StringIO()
                with patch("sys.stdout", output):
                    self.assertEqual(main(["generate", "--pending", "--dry-run"]), 0)
                text = output.getvalue()
                self.assertIn(POEM, text)
                self.assertIn(VISUAL, text)
                self.assertIn(prompt_hash(VISUAL), text)
                self.assertIn("House style: painterly", text)
                self.assertIn("Model: Z-Image Turbo", text)
                self.assertIn("Composition: wrapped", text)
                self.assertIn("Abstraction: surreal", text)
                self.assertIn("Method: interpreted", text)
                self.assertIn("Resolution: 1600x1600", text)
                self.assertIn("Export: print-suitable", text)
                self.assertIn("The poem stays pending.", text)
                self.assertNotIn("The following text:", text)
                plain = StringIO()
                with patch("sys.stdout", plain):
                    self.assertEqual(main(["generate", "--pending", "--dry-run", "--style", "none"]), 0)
                self.assertIn(prompt_hash(POEM), plain.getvalue())
                self.assertIn("House style: off", plain.getvalue())
                self.assertIn("Method: direct", plain.getvalue())
                other = StringIO()
                with patch("sys.stdout", other):
                    self.assertEqual(
                        main(
                            [
                                "generate",
                                "--pending",
                                "--dry-run",
                                "--style",
                                "symbolic",
                                "--composition",
                                "style_text",
                            ]
                        ),
                        0,
                    )
                self.assertIn(prompt_hash(VISUAL), other.getvalue())
                self.assertIn(PRESETS["symbolic"], other.getvalue())
                self.assertNotIn("The following text:", other.getvalue())
            self.assertEqual(fake.released, 2)
            with connect(self.database) as connection:
                count = connection.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
                status = connection.execute(
                    "SELECT status FROM selection_history"
                ).fetchone()["status"]
            self.assertEqual(count, 0)
            self.assertEqual(status, "pending")
            error = StringIO()
            with patch("sys.stderr", error):
                self.assertEqual(main(["generate"]), 1)
            self.assertIn("--pending", error.getvalue())
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def test_a_square_1024_image_is_print_sized(self):
        runner = FakeRunner(size=(1024, 1024))
        record = generate_pending(
            self.database,
            self.settings,
            runner=runner,
            seed=31,
            interpreter=self.interpreter,
            width=1024,
            height=1024,
        )
        saved = json.loads(record.settings_json)
        self.assertEqual((record.width, record.height), (1024, 1024))
        self.assertTrue(saved["print_suitable"])
        sent = runner.calls[0][0]
        self.assertEqual(sent[sent.index("--width") + 1], "1024")
        self.assertEqual(sent[sent.index("--height") + 1], "1024")

    def test_a_square_2400_image_meets_the_print_target(self):
        runner = FakeRunner(size=(2400, 2400))
        record = generate_pending(
            self.database,
            self.settings,
            runner=runner,
            seed=41,
            interpreter=self.interpreter,
            width=2400,
            height=2400,
        )
        saved = json.loads(record.settings_json)
        self.assertEqual((record.width, record.height), (2400, 2400))
        self.assertTrue(saved["print_suitable"])

    def test_an_unknown_model_and_a_bad_size_are_refused(self):
        with self.assertRaises(ValueError):
            generate_pending(
                self.database,
                self.settings,
                runner=FakeRunner(),
                seed=32,
                interpreter=self.interpreter,
                model_id="dreamshaper",
            )
        self.assertEqual(self.interpreter.calls, [])
        with self.assertRaises(GenerationError):
            generate_pending(
                self.database,
                replace(self.settings, width=100),
                runner=FakeRunner(),
                seed=33,
                interpreter=self.interpreter,
            )
        self.assertEqual(self.interpreter.calls, [])

    @unittest.skipUnless(ZIMAGE_PATH.is_file(), "Z-Image is not installed")
    def test_a_selected_model_keeps_its_negative_prompt(self):
        runner = FakeRunner()
        record = generate_pending(
            self.database,
            self.settings,
            runner=runner,
            seed=34,
            interpreter=self.interpreter,
            model_id="z-image",
        )
        sent = runner.calls[0][0]
        self.assertEqual(sent[sent.index("--prompt") + 1], VISUAL)
        self.assertIn("photorealistic", sent[sent.index("--negative-prompt") + 1])
        self.assertEqual(sent[sent.index("--steps") + 1], "35")
        self.assertEqual(sent[sent.index("--cfg-scale") + 1], "4.0")
        saved = json.loads(record.settings_json)
        self.assertEqual(saved["model_id"], "z-image")
        self.assertFalse(saved["print_suitable"])

    def test_the_production_default_is_an_eight_inch_print(self):
        previous = {name: os.environ.pop(name, None) for name in ("ANSKAR_SD_WIDTH", "ANSKAR_SD_HEIGHT")}
        try:
            settings = load_settings()
            self.assertEqual((settings.width, settings.height), (1600, 1600))
        finally:
            for name, value in previous.items():
                if value is not None:
                    os.environ[name] = value

    def test_settings_read_environment_overrides(self):
        previous = os.environ.get("ANSKAR_SD_WIDTH")
        os.environ["ANSKAR_SD_WIDTH"] = "768"
        try:
            self.assertEqual(load_settings().width, 768)
        finally:
            if previous is None:
                os.environ.pop("ANSKAR_SD_WIDTH", None)
            else:
                os.environ["ANSKAR_SD_WIDTH"] = previous


def _settings(root: Path, artworks: Path) -> GeneratorSettings:
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
        timeout_seconds=900,
        artworks_dir=artworks,
    )


def _write_png(path: Path, width: int, height: int) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr))


if __name__ == "__main__":
    unittest.main()
