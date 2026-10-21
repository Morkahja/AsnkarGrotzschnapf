import json
import math
import sqlite3
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from anskar.artwork.png import (
    classify_ppi,
    effective_ppi,
    label_png_dpi,
    largest_size,
    physical_inches,
    pixels_for_print,
    png_size,
    print_sized,
    print_sizes,
)
from anskar.artwork.settings import GeneratorSettings
from anskar.models import GUIDED_NEGATIVE_PROMPT, _turbo, _zimage
from anskar.printmaker import (
    PrintError,
    approve_artwork,
    meets_print_specification,
    orientation,
    preview_pixels,
)

POEM = "Little lamb, who made thee?\nDoes thou know who made thee?"


class GeometryTests(unittest.TestCase):
    def test_classifications_use_pixels_at_the_physical_size(self):
        self.assertEqual(orientation(1024, 1024), "square")
        self.assertEqual(orientation(1024, 768), "landscape")
        self.assertEqual(orientation(768, 1024), "portrait")
        self.assertEqual(effective_ppi(1024, 6), 171)
        self.assertEqual(effective_ppi(1024, 8), 128)
        self.assertEqual(effective_ppi(1600, 6), 267)
        self.assertEqual(effective_ppi(1600, 8), 200)
        self.assertEqual(effective_ppi(2048, 6), 341)
        self.assertEqual(effective_ppi(2048, 8), 256)
        six = _size(1024, 1024, 6)
        eight = _size(1024, 1024, 8)
        standard = _size(1600, 1600, 8)
        self.assertEqual(six["classification_label"], "Acceptable")
        self.assertTrue(six["suitable"])
        self.assertFalse(six["recommended"])
        self.assertEqual(eight["classification_label"], "Not print-ready")
        self.assertFalse(eight["suitable"])
        self.assertEqual(standard["classification"], "standard")
        self.assertEqual(standard["classification_label"], "Recommended")
        self.assertEqual(classify_ppi(300)["classification"], "high")
        landscape = _size(1024, 768, 8)
        self.assertEqual((landscape["width_inches"], landscape["height_inches"]), (8.0, 6.0))
        self.assertFalse(landscape["square"])
        portrait = _size(768, 1024, 8)
        self.assertEqual((portrait["width_inches"], portrait["height_inches"]), (6.0, 8.0))
        self.assertEqual(pixels_for_print(8, 8), (1600, 1600))
        self.assertEqual(pixels_for_print(10, 10), (2000, 2000))
        self.assertEqual(pixels_for_print(12, 12), (2400, 2400))
        self.assertGreaterEqual(pixels_for_print(8.1, 8.1)[0], math.ceil(8.1 * 200))
        self.assertEqual(pixels_for_print(8.1, 8.1)[0] % 8, 0)
        self.assertEqual(preview_pixels(512, 512), (512, 512))
        self.assertEqual(preview_pixels(1600, 1600), (1080, 1080))
        self.assertEqual(preview_pixels(1600, 1200), (1080, 810))
        self.assertTrue(print_sized(1024, 1024))
        self.assertTrue(print_sized(1600, 1600))
        self.assertFalse(print_sized(512, 512))
        recommended = largest_size(print_sizes(1024, 1024), recommended=True)
        acceptable = largest_size(print_sizes(1024, 1024), recommended=False)
        self.assertEqual(recommended["long_inches"], 4)
        self.assertEqual(acceptable["long_inches"], 6)
        self.assertEqual(largest_size(print_sizes(1600, 1600), recommended=True)["long_inches"], 8)
        self.assertEqual(physical_inches(1024, 1024), (3.41, 3.41))
        self.assertFalse(meets_print_specification((1024, 1024), (1600, 1600), "interpolation"))
        self.assertTrue(meets_print_specification((1024, 1024), (1024, 1024), "none"))
        self.assertFalse(meets_print_specification((512, 512), (512, 512), "none"))

    def test_dpi_label_does_not_change_pixels(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.png"
            _write_png(path, 32, 48)
            label_png_dpi(path, 300)
            self.assertEqual(png_size(path), (32, 48))
            self.assertIn(b"pHYs", path.read_bytes())


class PackTests(unittest.TestCase):
    def test_approval_copies_the_original_and_prepares_a_larger_print(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            experiments = root / "experiments" / "lamb"
            experiments.mkdir(parents=True)
            image = experiments / "run.png"
            metadata = experiments / "run.json"
            _write_png(image, 1024, 1024)
            original = image.read_bytes()
            metadata.write_text(
                json.dumps(
                    {
                        "experiment": "lamb",
                        "seed": 21,
                        "prompt": "A lamb made of weather.",
                        "prompt_sha256": "abc12345ffff",
                        "source_text": POEM,
                        "body": POEM,
                        "width": 1024,
                        "height": 1024,
                        "steps": 4,
                        "cfg_scale": 1,
                        "sampler": "euler",
                        "model_id": "z-image-turbo",
                        "model_name": "Z-Image Turbo",
                        "abstraction": "surreal",
                        "style_preset": "painterly",
                        "house_style": True,
                        "method": "interpreted",
                        "negative_prompt": "",
                    }
                ),
                encoding="utf-8",
            )
            database = root / "archive.sqlite"
            _poem(database, POEM, commercial="yes", status="documented")
            runner = _Runner()
            manifest = approve_artwork(
                metadata,
                experiments=root / "experiments",
                database=database,
                settings=_settings(root),
                runner=runner,
                packs=root / "packs",
            )
            self.assertTrue(manifest["publication_ready"])
            self.assertFalse(manifest["published"])
            self.assertEqual(image.read_bytes(), original)
            pack = Path(manifest["pack_path"])
            self.assertEqual(png_size(pack / "original" / "artwork.png"), (1024, 1024))
            self.assertEqual(png_size(pack / "print" / "artwork.png"), (1024, 1024))
            self.assertEqual(png_size(pack / "etsy" / "print.png"), (1024, 1024))
            self.assertEqual(png_size(pack / "instagram" / "preview.png"), (1024, 1024))
            self.assertIn("William Blake", (pack / "instagram" / "caption.txt").read_text())
            listing = (pack / "etsy" / "listing.txt").read_text()
            self.assertIn("digital download", listing)
            self.assertIn("does not receive a physical print", listing)
            self.assertIn("does not add image detail", listing)
            self.assertIn("6 × 6 inches: 171 PPI, Acceptable.", listing)
            self.assertIn("8 × 8 inches: 128 PPI, Unsuitable.", listing)
            self.assertIn("1024×1024 pixels", listing)
            self.assertEqual(manifest["export_class"], "print")
            self.assertTrue(manifest["source_print_suitable"])
            self.assertTrue(manifest["print_specification_met"])
            self.assertEqual(manifest["upscale_method"], "none")
            self.assertEqual(manifest["resizing_method"], "none")
            self.assertIsNone(manifest["upscaling_method"])
            self.assertEqual(manifest["original_pixel_dimensions"], {"width": 1024, "height": 1024})
            self.assertEqual(manifest["export_pixel_dimensions"], {"width": 1024, "height": 1024})
            self.assertEqual(manifest["effective_ppi"], 256)
            self.assertEqual(manifest["quality_classification"], "standard")
            self.assertEqual(manifest["largest_acceptable_print_size"]["ppi"], 171)
            self.assertEqual(manifest["instagram_resizing_method"], "none")
            self.assertGreater(manifest["file_size"], 0)
            self.assertIn(POEM, (pack / "instagram" / "caption.txt").read_text())
            self.assertEqual(runner.sizes, [])
            with self.assertRaises(PrintError):
                approve_artwork(
                    metadata,
                    experiments=root / "experiments",
                    database=database,
                    settings=_settings(root),
                    runner=runner,
                    packs=root / "packs",
                )
            self.assertEqual(image.read_bytes(), original)

    def test_indented_archived_poem_matches_by_poem_id_and_exact_body(self):
        """Leading spaces in archived bodies must not break rights matching."""
        whitman = (
            "  O Captain! my Captain! our fearful trip is done,\n"
            "  The ship has weather’d every rack, the prize we sought is won,"
        )
        visual = (
            "A vast painterly expanse of indigo and crimson swirls like a storm "
            "over a shattered deck of light."
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            experiments = root / "experiments" / "captain"
            experiments.mkdir(parents=True)
            image = experiments / "run.png"
            metadata = experiments / "run.json"
            _write_png(image, 1600, 1600)
            metadata.write_text(
                json.dumps(
                    {
                        "experiment": "production",
                        "poem_id": 1,
                        "selection_id": 1,
                        "title": "O Captain! My Captain!",
                        "author": "Walt Whitman",
                        "seed": 679240630,
                        "prompt": visual,
                        "visual_prompt": visual,
                        "prompt_sha256": "3966d2ce91f883e8de55bb86a178433ec48dbbe3eececfdf526ca10f640f1400",
                        "source_text": whitman,
                        "body": whitman,
                        "width": 1600,
                        "height": 1600,
                        "model_id": "z-image-turbo",
                        "model_name": "Z-Image Turbo",
                        "abstraction": "surreal",
                        "style_preset": "painterly",
                        "house_style": True,
                        "method": "interpreted",
                    }
                ),
                encoding="utf-8",
            )
            database = root / "archive.sqlite"
            _poem(
                database,
                whitman,
                commercial="yes",
                status="documented",
                title="O Captain! My Captain!",
                author="Walt Whitman",
            )
            manifest = approve_artwork(
                metadata,
                experiments=root / "experiments",
                database=database,
                settings=_settings(root),
                runner=_Runner(),
                packs=root / "packs",
            )
            self.assertEqual(png_size(image), (1600, 1600))
            self.assertTrue(manifest["publication_ready"])
            self.assertEqual(manifest["rights"]["poem_id"], 1)
            self.assertEqual(manifest["rights"]["title"], "O Captain! My Captain!")
            self.assertEqual(manifest["source_text"], whitman)
            self.assertEqual(manifest["visual_prompt"], visual)
            self.assertNotEqual(manifest["source_text"], manifest["visual_prompt"])
            self.assertEqual(png_size(Path(manifest["pack_path"]) / "print" / "artwork.png"), (1600, 1600))

            # Visual prompt alone must never clear rights.
            from anskar.printmaker import _rights

            refused = _rights(database, visual, poem_id=1, visual_prompt=visual)
            self.assertFalse(refused["matched"])
            refused_body = _rights(database, visual, poem_id=None, visual_prompt=visual)
            self.assertFalse(refused_body["matched"])

    def test_non_ready_pack_can_be_rebuilt_after_rights_fix(self):
        whitman = "  O Captain! my Captain! our fearful trip is done,"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            experiments = root / "experiments" / "captain"
            experiments.mkdir(parents=True)
            image = experiments / "run.png"
            metadata = experiments / "run.json"
            _write_png(image, 1600, 1600)
            record = {
                "experiment": "production",
                "poem_id": 1,
                "seed": 9,
                "prompt": "painterly storm light",
                "visual_prompt": "painterly storm light",
                "prompt_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "source_text": whitman,
                "body": whitman,
                "width": 1600,
                "height": 1600,
                "model_id": "z-image-turbo",
                "model_name": "Z-Image Turbo",
                "method": "interpreted",
            }
            metadata.write_text(json.dumps(record), encoding="utf-8")
            database = root / "archive.sqlite"
            packs = root / "packs"
            # First pack with empty archive → not ready.
            _poem(database, "unrelated text", commercial="no", status="unverified")
            first = approve_artwork(
                metadata,
                experiments=root / "experiments",
                database=database,
                settings=_settings(root),
                runner=_Runner(),
                packs=packs,
            )
            self.assertFalse(first["publication_ready"])
            pack_path = Path(first["pack_path"])
            self.assertTrue(pack_path.is_dir())
            # Fix archive and rebuild the same pack id without regenerating the image.
            with sqlite3.connect(database) as connection:
                connection.execute("DELETE FROM poems")
                connection.execute(
                    "INSERT INTO poems (title, author, body, rights_verification_status, commercial_use_sweden) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("O Captain! My Captain!", "Walt Whitman", whitman, "documented", "yes"),
                )
            second = approve_artwork(
                metadata,
                experiments=root / "experiments",
                database=database,
                settings=_settings(root),
                runner=_Runner(),
                packs=packs,
            )
            self.assertTrue(second["publication_ready"])
            self.assertEqual(second["artwork_id"], first["artwork_id"])
            self.assertEqual(png_size(image), (1600, 1600))

    def test_an_undocumented_text_is_not_publication_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "experiments" / "note"
            folder.mkdir(parents=True)
            image = folder / "run.png"
            _write_png(image, 1024, 768)
            metadata = folder / "run.json"
            metadata.write_text(
                json.dumps(
                    {
                        "experiment": "note",
                        "seed": 3,
                        "prompt": "A field of intervals.",
                        "prompt_sha256": "dddddddd",
                        "source_text": "An interval with no surface.",
                        "width": 1024,
                        "height": 768,
                        "steps": 4,
                        "cfg_scale": 1,
                        "sampler": "euler",
                        "model_id": "z-image-turbo",
                    }
                ),
                encoding="utf-8",
            )
            runner = _Runner()
            manifest = approve_artwork(
                metadata,
                experiments=root / "experiments",
                database=root / "missing.sqlite",
                settings=_settings(root),
                runner=runner,
                packs=root / "packs",
            )
            self.assertFalse(manifest["publication_ready"])
            self.assertEqual(runner.sizes, [])
            self.assertEqual(manifest["orientation"], "landscape")
            self.assertEqual(manifest["print_pixels"], {"width": 1024, "height": 768})
            self.assertEqual(png_size(Path(manifest["pack_path"]) / "print" / "artwork.png"), (1024, 768))
            self.assertEqual(png_size(Path(manifest["pack_path"]) / "instagram" / "preview.png"), (1024, 768))
            self.assertEqual(manifest["export_class"], "print")
            self.assertTrue(manifest["source_print_suitable"])
            self.assertTrue(manifest["print_specification_met"])
            listing = (Path(manifest["pack_path"]) / "etsy" / "listing.txt").read_text()
            self.assertIn("8.0 × 6.0 inches: 128 PPI, Unsuitable.", listing)
            self.assertIn("4.0 × 3.0 inches: 256 PPI, Recommended.", listing)
            self.assertNotIn("8 × 8", listing)

    def test_a_512_image_is_kept_and_is_not_print_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "experiments" / "small"
            folder.mkdir(parents=True)
            image = folder / "run.png"
            _write_png(image, 512, 512)
            original = image.read_bytes()
            metadata = folder / "run.json"
            metadata.write_text(
                json.dumps(
                    {
                        "experiment": "small",
                        "seed": 9,
                        "prompt": "A sphere of woven light.",
                        "prompt_sha256": "eeeeeeee",
                        "source_text": POEM,
                        "width": 512,
                        "height": 512,
                        "steps": 8,
                        "cfg_scale": 1,
                        "sampler": "euler",
                        "model_id": "z-image-turbo",
                    }
                ),
                encoding="utf-8",
            )
            _poem(root / "archive.sqlite", POEM, commercial="yes", status="documented")
            manifest = approve_artwork(
                metadata,
                experiments=root / "experiments",
                database=root / "archive.sqlite",
                settings=_settings(root),
                runner=_Runner(returncode=1),
                packs=root / "packs",
            )
            pack = Path(manifest["pack_path"])
            self.assertFalse(manifest["publication_ready"])
            self.assertTrue(manifest["rights_cleared"])
            self.assertFalse(manifest["print_specification_met"])
            self.assertEqual(manifest["upscale_method"], "none")
            self.assertEqual(manifest["quality_classification"], "low")
            self.assertIsNone(manifest["largest_acceptable_print_size"])
            self.assertEqual(png_size(pack / "print" / "artwork.png"), (512, 512))
            self.assertEqual(png_size(pack / "instagram" / "preview.png"), (512, 512))
            self.assertEqual(image.read_bytes(), original)
            listing = (pack / "etsy" / "listing.txt").read_text()
            self.assertIn("4 × 4 inches: 128 PPI, Unsuitable.", listing)
            self.assertIn("not resized or upscaled", listing)
            self.assertNotIn("2400", listing)

    def test_a_damaged_file_and_an_empty_size_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "experiments" / "broken"
            folder.mkdir(parents=True)
            image = folder / "run.png"
            image.write_bytes(b"not a png")
            metadata = folder / "run.json"
            metadata.write_text(
                json.dumps({"experiment": "broken", "seed": 1, "prompt_sha256": "abcd"}),
                encoding="utf-8",
            )
            with self.assertRaises(PrintError):
                approve_artwork(
                    metadata,
                    experiments=root / "experiments",
                    database=root / "missing.sqlite",
                    settings=_settings(root),
                    runner=_Runner(),
                    packs=root / "packs",
                )
            self.assertFalse((root / "packs").exists())
        with self.assertRaises(ValueError):
            print_sizes(0, 1024)

    def test_a_path_outside_the_experiments_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "other.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaises(PrintError):
                approve_artwork(
                    outside,
                    experiments=root / "experiments",
                    database=root / "missing.sqlite",
                    settings=_settings(root),
                    runner=_Runner(),
                    packs=root / "packs",
                )


class NegativeTests(unittest.TestCase):
    def test_photorealism_negatives_skip_turbo(self):
        root = Path(tempfile.mkdtemp())
        settings = _settings(root)
        turbo = _turbo(settings)
        guided = _zimage(settings)
        self.assertFalse(turbo.negative_enabled)
        self.assertEqual(turbo.negative_prompt, "")
        self.assertTrue(guided.negative_enabled)
        self.assertEqual(guided.negative_prompt, GUIDED_NEGATIVE_PROMPT)
        self.assertIn("photorealistic", guided.negative_prompt)

    def test_dreamshaper_commercial_use_blocks_publication(self):
        from anskar.models import DREAMSHAPER_PATH, DREAMSHAPER_SHA256, _dreamshaper

        if not DREAMSHAPER_PATH.is_file():
            self.skipTest("DreamShaper XL is not installed")
        profile = _dreamshaper(_settings(Path(tempfile.mkdtemp())))
        self.assertEqual(profile.commercial_use, "unverified")
        self.assertEqual(profile.sampler, "euler_a")
        self.assertEqual(profile.cfg_scale, 5.5)
        self.assertTrue(profile.vae_tiling)
        self.assertTrue(profile.offload_to_cpu)
        self.assertEqual(profile.model_checksum, DREAMSHAPER_SHA256)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "experiments" / "dream"
            folder.mkdir(parents=True)
            image = folder / "run.png"
            _write_png(image, 1024, 1024)
            metadata = folder / "run.json"
            metadata.write_text(
                json.dumps(
                    {
                        "experiment": "dream",
                        "seed": 9,
                        "prompt": "A woven sphere of light.",
                        "prompt_sha256": "deadbeefcafe0001",
                        "source_text": POEM,
                        "body": POEM,
                        "width": 1024,
                        "height": 1024,
                        "steps": 30,
                        "cfg_scale": 5.5,
                        "sampler": "euler_a",
                        "model_id": "dreamshaper-xl",
                        "model_name": "DreamShaper XL",
                        "commercial_use": "unverified",
                        "model_checksum": DREAMSHAPER_SHA256,
                        "abstraction": "surreal",
                        "style_preset": "painterly",
                        "method": "interpreted",
                        "negative_prompt": "text, letters",
                    }
                ),
                encoding="utf-8",
            )
            database = root / "archive.sqlite"
            _poem(database, POEM, commercial="yes", status="documented")
            manifest = approve_artwork(
                metadata,
                experiments=root / "experiments",
                database=database,
                settings=_settings(root),
                runner=_Runner(),
                packs=root / "packs",
            )
            self.assertTrue(manifest["rights_cleared"])
            self.assertTrue(manifest["print_specification_met"])
            self.assertFalse(manifest["model_commercial_cleared"])
            self.assertEqual(manifest["model_commercial_use"], "unverified")
            self.assertFalse(manifest["publication_ready"])
            listing = (Path(manifest["pack_path"]) / "etsy" / "listing.txt").read_text()
            self.assertIn("Commercial use: Unverified", listing)
            self.assertIn("not publication-ready for Etsy", listing)


def _size(width: int, height: int, long_inches: int) -> dict:
    for size in print_sizes(width, height):
        if size["long_inches"] == long_inches:
            return size
    raise AssertionError(f"No {long_inches}-inch size for {width}x{height}")


def _poem(
    database: Path,
    body: str,
    *,
    commercial: str,
    status: str,
    title: str = "THE LAMB",
    author: str = "William Blake",
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS poems (
                id INTEGER PRIMARY KEY,
                title TEXT,
                author TEXT,
                body TEXT,
                rights_verification_status TEXT,
                commercial_use_sweden TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO poems (title, author, body, rights_verification_status, commercial_use_sweden) VALUES (?, ?, ?, ?, ?)",
            (title, author, body, status, commercial),
        )


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


class _Runner:
    def __init__(self, *, returncode=0):
        self.sizes = []
        self.commands = []
        self.returncode = returncode

    def run(self, args, *, timeout):
        self.commands.append(list(args))
        width = int(args[args.index("--width") + 1])
        height = int(args[args.index("--height") + 1])
        self.sizes.append((width, height))
        if self.returncode == 0:
            _write_png(Path(args[args.index("--output") + 1]), width, height)
        return subprocess.CompletedProcess(args, self.returncode, b"", b"out of memory")


def _write_png(path: Path, width: int, height: int) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b""))


if __name__ == "__main__":
    unittest.main()
