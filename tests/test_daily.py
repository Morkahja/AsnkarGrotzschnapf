import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from anskar.archive.importer import import_collection
from anskar.archive.selection import select_poem
from anskar.artwork.generator import ArtworkRecord
from anskar.daily.direction import assert_usable_surreal_prompt, reinforce_production_prompt
from anskar.daily.errors import DailyError
from anskar.daily.lock import exclusive_lock
from anskar.daily.pipeline import daily_run, list_runs, resume_run, run_status
from anskar.daily.schedule import install_units
from anskar.db import connect
from anskar.etsy.state import PENDING_VERIFICATION, READY, load_etsy_state, set_etsy_state
from anskar.instagram.errors import InstagramError

POEM_BODY = "Little lamb, who made thee?\nDoes thou know who made thee?"
POEM_BODY_2 = "Tiger tiger burning bright\nIn the forests of the night"


class DailyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.archive = self.root / "archive.sqlite"
        self.daily_db = self.root / "daily.sqlite"
        self.packs = self.root / "packs"
        self.artworks = self.root / "artworks"
        self.lock = self.root / "daily.lock"
        self.etsy_state = self.root / "etsy-state.json"
        self.packs.mkdir()
        self.artworks.mkdir()
        self._seed_archive()
        set_etsy_state(PENDING_VERIFICATION, path=self.etsy_state)
        os.environ["ANSKAR_NOTIFY"] = "0"

    def tearDown(self):
        self.temporary.cleanup()

    def test_instagram_success_completes_while_etsy_pending_verification(self):
        etsy_calls: list[str] = []

        def publish_etsy(*args, **kwargs):
            etsy_calls.append("called")
            raise AssertionError("Etsy API must not be called while pending verification")

        with patch("anskar.daily.pipeline.load_etsy_state", return_value=PENDING_VERIFICATION):
            with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
                result = daily_run(
                    archive_db=self.archive,
                    daily_db=self.daily_db,
                    packs=self.packs,
                    artworks=self.artworks,
                    generate=lambda *a, **k: self._fake_artwork(self.archive),
                    prepare_pack=lambda metadata, **k: self._fake_pack(metadata),
                    publish_instagram=lambda pack_id, **k: {
                        "published": True,
                        "media_id": "ig-1",
                        "permalink": "https://www.instagram.com/p/example/",
                        "pack_id": pack_id,
                    },
                    publish_etsy=publish_etsy,
                    lock=self.lock,
                )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["daily_status"], "completed")
        self.assertEqual(result["instagram_status"], "published")
        self.assertEqual(result["etsy_status"], "pending_shop_activation")
        self.assertEqual(result["artwork_status"], "generated")
        self.assertEqual(etsy_calls, [])
        queue = self.packs / result["pack_id"] / "etsy" / "queue.json"
        self.assertTrue(queue.is_file())
        self.assertEqual(json.loads(queue.read_text())["status"], "pending_shop_activation")
        with connect(self.archive) as connection:
            confirmed = connection.execute(
                "SELECT 1 FROM selection_history WHERE status = 'confirmed'"
            ).fetchone()
            pending = connection.execute(
                "SELECT 1 FROM selection_history WHERE status = 'pending'"
            ).fetchone()
        self.assertIsNotNone(confirmed)
        self.assertIsNone(pending)

    def test_daily_run_completes_without_etsy_credentials(self):
        with patch("anskar.daily.pipeline.load_etsy_state", return_value=PENDING_VERIFICATION):
            with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
                result = daily_run(
                    archive_db=self.archive,
                    daily_db=self.daily_db,
                    packs=self.packs,
                    artworks=self.artworks,
                    generate=lambda *a, **k: self._fake_artwork(self.archive),
                    prepare_pack=lambda metadata, **k: self._fake_pack(metadata),
                    publish_instagram=lambda pack_id, **k: {
                        "published": True,
                        "media_id": "ig-cred",
                        "permalink": "https://www.instagram.com/p/c/",
                        "pack_id": pack_id,
                    },
                    lock=self.lock,
                )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["etsy_status"], "pending_shop_activation")

    def test_instagram_failure_keeps_poem_pending(self):
        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            with self.assertRaises(DailyError):
                daily_run(
                    archive_db=self.archive,
                    daily_db=self.daily_db,
                    packs=self.packs,
                    artworks=self.artworks,
                    generate=lambda *a, **k: self._fake_artwork(self.archive),
                    prepare_pack=lambda metadata, **k: self._fake_pack(metadata),
                    publish_instagram=lambda *a, **k: (_ for _ in ()).throw(
                        InstagramError("rate limit", kind="api")
                    ),
                    lock=self.lock,
                )
        status = run_status(daily_db=self.daily_db)
        self.assertEqual(status["active"]["status"], "pack_prepared")
        with connect(self.archive) as connection:
            pending = connection.execute(
                "SELECT 1 FROM selection_history WHERE status = 'pending'"
            ).fetchone()
        self.assertIsNotNone(pending)

    def test_resume_does_not_republish_instagram(self):
        ig_calls: list[str] = []

        def publish_instagram(pack_id, **kwargs):
            ig_calls.append(pack_id)
            return {
                "published": True,
                "media_id": "ig-resume",
                "permalink": "https://www.instagram.com/p/r/",
                "pack_id": pack_id,
            }

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            # Force stop after Instagram by simulating crash before complete:
            first = daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: self._fake_artwork(self.archive),
                prepare_pack=lambda metadata, **k: self._fake_pack(metadata),
                publish_instagram=publish_instagram,
                lock=self.lock,
            )
        self.assertEqual(first["status"], "completed")
        self.assertEqual(ig_calls, [first["pack_id"]])

        # Resume of completed run is a no-op and must not call Instagram again.
        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            again = resume_run(
                first["id"],
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                publish_instagram=lambda *a, **k: (_ for _ in ()).throw(AssertionError("dup")),
                lock=self.lock,
            )
        self.assertEqual(again["status"], "completed")
        self.assertEqual(len(ig_calls), 1)

    def test_next_run_selects_a_different_poem(self):
        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            first = daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: self._fake_artwork(self.archive),
                prepare_pack=lambda metadata, **k: self._fake_pack(metadata),
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-a",
                    "permalink": "https://www.instagram.com/p/a/",
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        # One live completion per UTC day is blocked; dry-run still allowed for the second poem.
        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            second = daily_run(
                dry_run=True,
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: self._fake_artwork(self.archive, seed=99),
                prepare_pack=lambda metadata, **k: self._fake_pack(metadata, suffix="b"),
                publish_instagram=lambda pack_id, **k: {
                    "dry_run": True,
                    "published": False,
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        self.assertIn(first["title"], {"THE LAMB", "THE TIGER"})
        self.assertIn(second["title"], {"THE LAMB", "THE TIGER"})
        self.assertNotEqual(first["poem_id"], second["poem_id"])
        self.assertNotEqual(first["title"], second["title"])

    def test_generation_failure_marks_failed(self):
        from anskar.artwork.generator import GenerationError

        with self.assertRaises(DailyError):
            daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: (_ for _ in ()).throw(GenerationError("sd-cli failed")),
                prepare_pack=lambda *a, **k: (_ for _ in ()).throw(AssertionError("pack")),
                publish_instagram=lambda *a, **k: (_ for _ in ()).throw(AssertionError("ig")),
                lock=self.lock,
            )
        rows = list_runs(daily_db=self.daily_db)
        self.assertEqual(rows[0]["status"], "failed")

    def test_lock_blocks_overlapping_runs(self):
        with exclusive_lock(self.lock):
            with self.assertRaises(DailyError) as caught:
                with exclusive_lock(self.lock):
                    pass
            self.assertEqual(caught.exception.kind, "busy")

    def test_schedule_install_writes_units_without_enabling(self):
        source = self.root / "systemd"
        source.mkdir()
        (source / "anskar-daily.service").write_text(
            "WorkingDirectory=@PROJECT_ROOT@\n"
            "ExecStart=/usr/bin/systemd-inhibit --what=idle:sleep --who=Anskar "
            "--why=paint --mode=block @PYTHON@ -m anskar daily-run\n",
            encoding="utf-8",
        )
        (source / "anskar-daily.timer").write_text(
            "[Timer]\nOnCalendar=*-*-* 12:00:00\nPersistent=false\n",
            encoding="utf-8",
        )
        unit_dir = self.root / "user-units"
        with patch("anskar.daily.schedule.unit_source_dir", return_value=source):
            with patch.dict(os.environ, {"ANSKAR_SYSTEMD_USER_DIR": str(unit_dir)}):
                with patch("anskar.daily.schedule.shutil.which", return_value=None):
                    report = install_units(enable=False)
        self.assertTrue((unit_dir / "anskar-daily.service").is_file())
        self.assertIn("12:00:00", (unit_dir / "anskar-daily.timer").read_text(encoding="utf-8"))
        self.assertFalse(report["enabled_now"])

    def test_dry_run_does_not_mark_poem_used(self):
        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            result = daily_run(
                dry_run=True,
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: self._fake_artwork(self.archive),
                prepare_pack=lambda metadata, **k: self._fake_pack(metadata),
                publish_instagram=lambda pack_id, **k: {
                    "dry_run": True,
                    "published": False,
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["dry_run"])
        with connect(self.archive) as connection:
            pending = connection.execute(
                "SELECT 1 FROM selection_history WHERE status = 'pending'"
            ).fetchone()
            confirmed = connection.execute(
                "SELECT 1 FROM selection_history WHERE status = 'confirmed'"
            ).fetchone()
        self.assertIsNotNone(pending)
        self.assertIsNone(confirmed)

    def test_etsy_activation_remains_optional(self):
        self.assertEqual(load_etsy_state(self.etsy_state), PENDING_VERIFICATION)
        set_etsy_state(READY, path=self.etsy_state)
        self.assertEqual(load_etsy_state(self.etsy_state), READY)
        status = run_status(daily_db=self.daily_db)
        self.assertFalse(status["etsy_blocks_daily_completion"])
        self.assertTrue(status["poem_marked_used_after_instagram"])

    def test_production_direction_reinforces_and_rejects_literal_prompts(self):
        reinforced = reinforce_production_prompt("An impossible chamber of wool light")
        self.assertIn("Not a photograph", reinforced)
        assert_usable_surreal_prompt(reinforced)
        with self.assertRaises(DailyError):
            assert_usable_surreal_prompt(
                "A photorealistic lamb standing in a meadow under moonlight, portrait of a child"
            )

    def test_recovers_skipped_poem_with_unpublished_production_pack(self):
        """A skipped poem that still has a ready pack is re-selected for resume."""
        with connect(self.archive) as connection:
            selection = select_poem(connection)
            poem_id = selection.poem.id
            selection_id = selection.id
            title = selection.poem.title
            author = selection.poem.author
            from anskar.archive.selection import skip_pending as skip

            skip(connection)
        image = self._write_production_png(poem_id, selection_id, seed=55)
        self._insert_succeeded_artwork(
            poem_id=poem_id,
            selection_id=selection_id,
            title=title,
            author=author,
            seed=55,
            path=image,
            width=1600,
            height=1600,
        )
        pack = self._fake_pack(image.with_suffix(".json"), suffix="recover")
        generate_calls: list[str] = []

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            result = daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: generate_calls.append("x") or (_ for _ in ()).throw(
                    AssertionError("no gen")
                ),
                prepare_pack=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no pack")),
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-recover",
                    "permalink": "https://www.instagram.com/p/recover/",
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["poem_id"], poem_id)
        self.assertEqual(result["pack_id"], pack["artwork_id"])
        self.assertEqual(generate_calls, [])
        log = result.get("selection_log") or []
        self.assertTrue(any(entry.get("action") == "resume" for entry in log))

    def test_pending_with_compatible_artwork_resumes_without_regenerating(self):
        generate_calls: list[str] = []
        with connect(self.archive) as connection:
            selection = select_poem(connection)
            poem_id = selection.poem.id
            selection_id = selection.id
            title = selection.poem.title
            author = selection.poem.author
            body = selection.poem.body
        image = self._write_production_png(poem_id, selection_id, seed=42)
        self._insert_succeeded_artwork(
            poem_id=poem_id,
            selection_id=selection_id,
            title=title,
            author=author,
            seed=42,
            path=image,
            width=1600,
            height=1600,
        )
        pack = self._fake_pack(image.with_suffix(".json"), suffix="resume")

        def generate(*_a, **_k):
            generate_calls.append("called")
            raise AssertionError("must not regenerate")

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            result = daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=generate,
                prepare_pack=lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("pack already exists")
                ),
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-resume-art",
                    "permalink": "https://www.instagram.com/p/resume/",
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["poem_id"], poem_id)
        self.assertEqual(result["pack_id"], pack["artwork_id"])
        self.assertEqual(generate_calls, [])
        log = result.get("selection_log") or []
        self.assertTrue(any(entry.get("action") == "resume" for entry in log))
        self.assertTrue(image.is_file())

    def test_incompatible_legacy_artwork_skips_to_another_poem(self):
        generate_calls: list[int] = []
        with connect(self.archive) as connection:
            selection = select_poem(connection)
            legacy_id = selection.poem.id
            selection_id = selection.id
            title = selection.poem.title
            author = selection.poem.author
        legacy = self.artworks / f"poem-{legacy_id}-legacy.png"
        legacy.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x08\x00\x00\x00\x08"
            b"\x08\x02\x00\x00\x00K\xd3\x9b\x81\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        legacy.with_suffix(".json").write_text(
            json.dumps(
                {
                    "experiment": "production",
                    "poem_id": legacy_id,
                    "selection_id": selection_id,
                    "title": title,
                    "author": author,
                    "width": 8,
                    "height": 8,
                    "seed": 1,
                    "output_path": str(legacy),
                }
            ),
            encoding="utf-8",
        )
        self._insert_succeeded_artwork(
            poem_id=legacy_id,
            selection_id=selection_id,
            title=title,
            author=author,
            seed=1,
            path=legacy,
            width=8,
            height=8,
        )

        def generate(*_a, **_k):
            with connect(self.archive) as connection:
                pending = connection.execute(
                    "SELECT poem_id FROM selection_history WHERE status = 'pending'"
                ).fetchone()
            assert pending is not None
            generate_calls.append(int(pending[0]))
            return self._fake_artwork(self.archive, seed=88)

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            result = daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=generate,
                prepare_pack=lambda metadata, **k: self._fake_pack(metadata, suffix="new"),
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-skip",
                    "permalink": "https://www.instagram.com/p/skip/",
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        self.assertEqual(result["status"], "completed")
        self.assertNotEqual(result["poem_id"], legacy_id)
        self.assertEqual(generate_calls, [result["poem_id"]])
        self.assertTrue(legacy.is_file())
        log = result.get("selection_log") or []
        self.assertTrue(any(entry.get("action") == "skip_legacy" for entry in log))
        self.assertTrue(any(entry.get("action") == "generate" for entry in log))

    def test_no_eligible_poems_remaining_stops_cleanly(self):
        # Both poems have incompatible legacy artworks: cannot resume, cannot regenerate.
        with connect(self.archive) as connection:
            poems = list(
                connection.execute("SELECT id, title, author FROM poems ORDER BY id").fetchall()
            )
            for poem in poems:
                connection.execute(
                    """
                    INSERT INTO selection_history (poem_id, selected_at, skipped_at, status)
                    VALUES (?, '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:01+00:00', 'skipped')
                    """,
                    (poem["id"],),
                )
                selection_id = connection.execute(
                    "SELECT id FROM selection_history WHERE poem_id = ? ORDER BY id DESC LIMIT 1",
                    (poem["id"],),
                ).fetchone()[0]
                path = self.artworks / f"block-{poem['id']}.png"
                self._write_claimed_png(path, 64, 64)
                path.with_suffix(".json").write_text(
                    json.dumps(
                        {
                            "experiment": "production",
                            "poem_id": poem["id"],
                            "width": 64,
                            "height": 64,
                        }
                    ),
                    encoding="utf-8",
                )
                connection.execute(
                    """
                    INSERT INTO artworks (
                        poem_id, selection_id, title, author, prompt_sha256, model, seed,
                        width, height, settings_json, output_path, status, created_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, 'z-image-turbo', 1, 64, 64, '{}', ?, 'succeeded',
                              '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:01+00:00')
                    """,
                    (
                        poem["id"],
                        selection_id,
                        poem["title"],
                        poem["author"],
                        "c" * 64,
                        str(path),
                    ),
                )

        with self.assertRaises(DailyError) as caught:
            daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no gen")),
                prepare_pack=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no pack")),
                publish_instagram=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no ig")),
                lock=self.lock,
            )
        self.assertEqual(caught.exception.kind, "empty")
        rows = list_runs(daily_db=self.daily_db)
        self.assertEqual(rows[0]["status"], "failed")

    def test_failed_pack_rights_run_resumes_same_image(self):
        """A media failure after generation can resume without regenerating."""
        generate_calls: list[str] = []

        def generate(*_a, **_k):
            generate_calls.append("gen")
            return self._fake_artwork(self.archive)

        pack_calls = {"n": 0}

        def prepare_pack(metadata, **_k):
            pack_calls["n"] += 1
            if pack_calls["n"] == 1:
                raise DailyError(
                    "The production pack is not publication-ready. "
                    "The source text is not an archived poem with documented commercial-use rights.",
                    kind="media",
                )
            return self._fake_pack(metadata, suffix="fixed")

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            with self.assertRaises(DailyError) as caught:
                daily_run(
                    archive_db=self.archive,
                    daily_db=self.daily_db,
                    packs=self.packs,
                    artworks=self.artworks,
                    generate=generate,
                    prepare_pack=prepare_pack,
                    publish_instagram=lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError("ig")
                    ),
                    lock=self.lock,
                )
        self.assertEqual(caught.exception.kind, "media")
        self.assertEqual(generate_calls, ["gen"])
        status = run_status(daily_db=self.daily_db)
        self.assertIsNone(status["active"])
        failed = list_runs(daily_db=self.daily_db)[0]
        self.assertEqual(failed["status"], "failed")
        run_id = failed["id"]
        image_path = Path(str(failed["image_path"]))
        self.assertTrue(image_path.is_file())

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            result = resume_run(
                run_id,
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no regen")),
                prepare_pack=prepare_pack,
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-fixed",
                    "permalink": "https://www.instagram.com/p/fixed/",
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(generate_calls, ["gen"])
        self.assertEqual(pack_calls["n"], 2)
        self.assertTrue(image_path.is_file())

    def test_resume_reuses_existing_publication_ready_pack(self):
        """Resume with artwork + ready pack must not recreate the pack."""
        from anskar.daily.ledger import begin_run, connect as daily_connect, update_run
        from anskar.printmaker import artwork_id

        with connect(self.archive) as connection:
            selection = select_poem(connection)
            poem_id = selection.poem.id
            selection_id = selection.id
            title = selection.poem.title
            author = selection.poem.author
        image = self._write_production_png(poem_id, selection_id, seed=679240630)
        self._insert_succeeded_artwork(
            poem_id=poem_id,
            selection_id=selection_id,
            title=title,
            author=author,
            seed=679240630,
            path=image,
            width=1600,
            height=1600,
        )
        meta = image.with_suffix(".json")
        record = json.loads(meta.read_text(encoding="utf-8"))
        pack_id = artwork_id(record)
        digest = str(record.get("prompt_sha256") or "nosha")[:8]
        self._fake_pack(meta, suffix=digest)
        self.assertTrue((self.packs / pack_id / "manifest.json").is_file())

        with daily_connect(self.daily_db) as connection:
            run_id = begin_run(connection, dry_run=False)
            update_run(
                connection,
                run_id,
                status="failed",
                poem_id=poem_id,
                selection_id=selection_id,
                title=title,
                author=author,
                image_path=str(image),
                seed=679240630,
                artwork_status="generated",
                error_kind="media",
                error_message="The production pack is not publication-ready.",
            )

        pack_calls: list[str] = []
        ig_calls: list[str] = []

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            result = resume_run(
                run_id,
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no gen")),
                prepare_pack=lambda *a, **k: pack_calls.append("made")
                or (_ for _ in ()).throw(AssertionError("must reuse pack")),
                publish_instagram=lambda pid, **k: (
                    ig_calls.append(pid)
                    or {
                        "published": True,
                        "media_id": "ig-reuse-pack",
                        "permalink": "https://www.instagram.com/p/reuse/",
                        "pack_id": pid,
                    }
                ),
                lock=self.lock,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["pack_id"], pack_id)
        self.assertEqual(result["instagram_status"], "published")
        self.assertEqual(result["instagram_media_id"], "ig-reuse-pack")
        self.assertEqual(pack_calls, [])
        self.assertEqual(ig_calls, [pack_id])
        self.assertTrue(image.is_file())
        self.assertTrue((self.packs / pack_id / "manifest.json").is_file())

    def test_crash_recovery_resumes_pack_without_new_artwork(self):
        generate_calls: list[str] = []

        def generate(*_a, **_k):
            generate_calls.append("gen")
            return self._fake_artwork(self.archive)

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            with self.assertRaises(DailyError):
                daily_run(
                    archive_db=self.archive,
                    daily_db=self.daily_db,
                    packs=self.packs,
                    artworks=self.artworks,
                    generate=generate,
                    prepare_pack=lambda metadata, **k: self._fake_pack(metadata),
                    publish_instagram=lambda *a, **k: (_ for _ in ()).throw(
                        InstagramError("crash", kind="api")
                    ),
                    lock=self.lock,
                )
        self.assertEqual(len(generate_calls), 1)
        status = run_status(daily_db=self.daily_db)
        run_id = status["active"]["id"]

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            result = resume_run(
                run_id,
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no regen")),
                prepare_pack=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no repack")),
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-crash",
                    "permalink": "https://www.instagram.com/p/crash/",
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(generate_calls), 1)

    def test_existing_production_artwork_is_packed_without_regenerating(self):
        with connect(self.archive) as connection:
            poem = connection.execute(
                "SELECT id, title, author FROM poems ORDER BY id LIMIT 1"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO selection_history (poem_id, selected_at, skipped_at, status)
                VALUES (?, '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:01+00:00', 'skipped')
                """,
                (poem["id"],),
            )
            selection_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        image = self._write_production_png(poem["id"], selection_id, seed=42)
        self._insert_succeeded_artwork(
            poem_id=poem["id"],
            selection_id=selection_id,
            title=poem["title"],
            author=poem["author"],
            seed=42,
            path=image,
            width=1600,
            height=1600,
        )
        packed: list[Path] = []

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            result = daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no gen")),
                prepare_pack=lambda metadata, **k: packed.append(metadata) or self._fake_pack(metadata),
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-existing",
                    "permalink": "https://www.instagram.com/p/existing/",
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["poem_id"], poem["id"])
        self.assertEqual(result["instagram_permalink"], "https://www.instagram.com/p/existing/")
        self.assertEqual(result["etsy_status"], "pending_shop_activation")
        self.assertEqual(len(packed), 1)
        self.assertTrue(image.is_file())

    def test_used_mark_does_not_confirm_a_different_pending_poem(self):
        from anskar.daily.ledger import begin_run, connect as daily_connect, update_run

        with connect(self.archive) as connection:
            poems = list(connection.execute("SELECT id FROM poems ORDER BY id").fetchall())
            first_id = int(poems[0]["id"])
            second_id = int(poems[1]["id"])
            connection.execute(
                """
                INSERT INTO selection_history (poem_id, selected_at, status)
                VALUES (?, '2020-01-01T00:00:00+00:00', 'pending')
                """,
                (first_id,),
            )
            connection.execute(
                """
                INSERT INTO selection_history (poem_id, selected_at, skipped_at, status)
                VALUES (?, '2020-01-02T00:00:00+00:00', '2020-01-02T00:00:01+00:00', 'skipped')
                """,
                (second_id,),
            )
            skipped_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        image = self._write_production_png(second_id, skipped_id, seed=7)
        self._fake_pack(image.with_suffix(".json"), suffix="kept")

        with daily_connect(self.daily_db) as connection:
            run_id = begin_run(connection, dry_run=False)
            update_run(
                connection,
                run_id,
                status="instagram_published",
                poem_id=second_id,
                selection_id=skipped_id,
                pack_id=f"production-poem-{second_id}-seed-7-kept",
                image_path=str(image),
                instagram_status="published",
                instagram_media_id="ig-kept",
                instagram_permalink="https://www.instagram.com/p/kept/",
            )

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            result = resume_run(
                run_id,
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                publish_instagram=lambda *a, **k: (_ for _ in ()).throw(AssertionError("dup")),
                lock=self.lock,
            )
        self.assertEqual(result["status"], "completed")
        with connect(self.archive) as connection:
            pending = connection.execute(
                "SELECT poem_id FROM selection_history WHERE status = 'pending'"
            ).fetchone()
            used = connection.execute(
                "SELECT status FROM selection_history WHERE id = ?",
                (skipped_id,),
            ).fetchone()
        self.assertEqual(int(pending["poem_id"]), first_id)
        self.assertEqual(used["status"], "confirmed")

    def test_second_live_run_same_day_blocked(self):
        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            first = daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: self._fake_artwork(self.archive),
                prepare_pack=lambda metadata, **k: self._fake_pack(metadata),
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-day",
                    "permalink": "https://www.instagram.com/p/day/",
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        self.assertEqual(first["status"], "completed")
        with self.assertRaises(DailyError) as caught:
            daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("second")),
                prepare_pack=lambda *a, **k: (_ for _ in ()).throw(AssertionError("second")),
                publish_instagram=lambda *a, **k: (_ for _ in ()).throw(AssertionError("second")),
                lock=self.lock,
            )
        self.assertEqual(caught.exception.kind, "busy")

    def test_rehearsal_adds_an_artwork_without_using_the_noon_slot(self):
        etsy_calls: list[str] = []

        def publish_etsy(*_args, **_kwargs):
            etsy_calls.append("live")
            return {"listing_id": "should-not-happen"}

        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            first = daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: self._fake_artwork(self.archive, seed=1),
                prepare_pack=lambda metadata, **k: self._fake_pack(metadata, suffix="noon"),
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-noon",
                    "permalink": "https://www.instagram.com/p/noon/",
                    "pack_id": pack_id,
                },
                lock=self.lock,
            )
        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=True):
            extra = daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                allow_test=True,
                queue_etsy_only=True,
                generate=lambda *a, **k: self._fake_artwork(self.archive, seed=2),
                prepare_pack=lambda metadata, **k: self._fake_pack(metadata, suffix="test"),
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-test",
                    "permalink": "https://www.instagram.com/p/test/",
                    "pack_id": pack_id,
                },
                publish_etsy=publish_etsy,
                lock=self.lock,
            )
        self.assertEqual(first["status"], "completed")
        self.assertEqual(extra["status"], "completed")
        self.assertNotEqual(first["poem_id"], extra["poem_id"])
        self.assertNotEqual(first["pack_id"], extra["pack_id"])
        self.assertEqual(extra["instagram_permalink"], "https://www.instagram.com/p/test/")
        self.assertEqual(extra["etsy_status"], "pending_shop_activation")
        self.assertEqual(etsy_calls, [])
        with self.assertRaises(DailyError) as caught:
            daily_run(
                archive_db=self.archive,
                daily_db=self.daily_db,
                packs=self.packs,
                artworks=self.artworks,
                generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("noon")),
                lock=self.lock,
            )
        self.assertEqual(caught.exception.kind, "busy")

    def _write_claimed_png(self, path: Path, width: int, height: int) -> Path:
        import struct
        import zlib

        path.parent.mkdir(parents=True, exist_ok=True)
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        path.write_bytes(
            b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")
        )
        return path

    def _write_production_png(self, poem_id: int, selection_id: int, *, seed: int) -> Path:
        image = self.artworks / f"poem-{poem_id}-selection-{selection_id}-seed-{seed}.png"
        self._write_claimed_png(image, 1600, 1600)
        with connect(self.archive) as connection:
            poem = connection.execute(
                "SELECT title, author, body FROM poems WHERE id = ?", (poem_id,)
            ).fetchone()
        meta = {
            "experiment": "production",
            "poem_id": poem_id,
            "selection_id": selection_id,
            "title": poem["title"],
            "author": poem["author"],
            "source_text": poem["body"],
            "body": poem["body"],
            "prompt": "impossible chamber of wool-light",
            "visual_prompt": "impossible chamber of wool-light",
            "prompt_sha256": "d" * 64,
            "seed": seed,
            "width": 1600,
            "height": 1600,
            "model_id": "z-image-turbo",
            "output_path": str(image),
        }
        image.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
        return image

    def _insert_succeeded_artwork(
        self,
        *,
        poem_id: int,
        selection_id: int,
        title: str,
        author: str,
        seed: int,
        path: Path,
        width: int,
        height: int,
    ) -> None:
        with connect(self.archive) as connection:
            connection.execute(
                """
                INSERT INTO artworks (
                    poem_id, selection_id, title, author, prompt_sha256, model, seed,
                    width, height, settings_json, output_path, status, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, 'z-image-turbo', ?, ?, ?, '{}', ?, 'succeeded',
                          '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:01+00:00')
                """,
                (poem_id, selection_id, title, author, "e" * 64, seed, width, height, str(path)),
            )

    def _seed_archive(self) -> None:
        collection = self.root / "poems.json"
        collection.write_text(
            json.dumps(
                {
                    "collection_id": "test",
                    "poems": [
                        {
                            "title": "THE LAMB",
                            "author": "William Blake",
                            "author_death_year": 1827,
                            "author_death_year_source": "Britannica",
                            "original_language": "en",
                            "text_language": "en",
                            "body": POEM_BODY,
                            "source_url": "https://example.test/lamb",
                            "license_name": "Public Domain",
                            "license_info": "Author died in 1827.",
                            "rights_verification_status": "documented",
                        },
                        {
                            "title": "THE TIGER",
                            "author": "William Blake",
                            "author_death_year": 1827,
                            "author_death_year_source": "Britannica",
                            "original_language": "en",
                            "text_language": "en",
                            "body": POEM_BODY_2,
                            "source_url": "https://example.test/tiger",
                            "license_name": "Public Domain",
                            "license_info": "Author died in 1827.",
                            "rights_verification_status": "documented",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with connect(self.archive) as connection:
            import_collection(connection, collection, on=date.today())

    def _fake_artwork(self, database: Path, *, poem_id: int = 1, seed: int = 7) -> ArtworkRecord:
        with connect(database) as connection:
            selection = select_poem(connection)
            poem_id = selection.poem.id
            selection_id = selection.id
            title = selection.poem.title
            author = selection.poem.author
            body = selection.poem.body
        image = self.artworks / f"poem-{poem_id}-seed-{seed}.png"
        image.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x08\x00\x00\x00\x08"
            b"\x08\x02\x00\x00\x00K\xd3\x9b\x81\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        meta = {
            "experiment": "production",
            "poem_id": poem_id,
            "selection_id": selection_id,
            "title": title,
            "author": author,
            "source_text": body,
            "body": body,
            "prompt": "impossible chamber of wool-light and floating glyphs",
            "visual_prompt": "impossible chamber of wool-light and floating glyphs",
            "prompt_sha256": "a" * 64,
            "source_sha256": "b" * 64,
            "seed": seed,
            "width": 1600,
            "height": 1600,
            "model_id": "z-image-turbo",
            "model_name": "Z-Image Turbo",
            "abstraction": "surreal",
            "style_preset": "painterly",
            "method": "interpreted",
            "output_path": str(image),
        }
        image.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
        return ArtworkRecord(
            id=seed,
            poem_id=poem_id,
            selection_id=selection_id,
            title=title,
            author=author,
            prompt_sha256="a" * 64,
            model="z-image-turbo",
            seed=seed,
            width=1600,
            height=1600,
            duration_seconds=1.0,
            settings_json=json.dumps({"source_text": body, "house_style": True}),
            output_path=str(image),
            status="succeeded",
            error=None,
        )

    def _fake_pack(self, metadata: Path, *, suffix: str = "a") -> dict:
        record = json.loads(metadata.read_text(encoding="utf-8"))
        pack_id = f"production-poem-{record.get('poem_id')}-seed-{record.get('seed')}-{suffix}"
        folder = self.packs / pack_id
        (folder / "etsy").mkdir(parents=True)
        (folder / "instagram").mkdir(parents=True)
        (folder / "print").mkdir(parents=True)
        png = metadata.with_suffix(".png").read_bytes()
        (folder / "etsy" / "print.png").write_bytes(png)
        (folder / "etsy" / "preview.png").write_bytes(png)
        (folder / "instagram" / "preview.png").write_bytes(png)
        (folder / "print" / "artwork.png").write_bytes(png)
        manifest = {
            "artwork_id": pack_id,
            "publication_ready": True,
            "digital_download": True,
            "published": False,
            "source_text": record.get("source_text"),
            "rights": {
                "matched": True,
                "publication_ready": True,
                "title": record.get("title"),
                "author": record.get("author"),
                "poem_id": record.get("poem_id"),
            },
            "print_pixels": {"width": 1600, "height": 1600},
            "orientation": "square",
            "style_preset": "painterly",
            "abstraction": "surreal",
            "ai_disclosure": "This image was generated locally by software.",
        }
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        manifest["pack_path"] = str(folder)
        return manifest


class EtsyListingTests(unittest.TestCase):
    def test_listing_fields_include_disclosure_and_digital_type(self):
        from anskar.etsy.listing import listing_fields

        fields = listing_fields(
            {
                "rights": {"title": "THE LAMB", "author": "William Blake"},
                "print_pixels": {"width": 1600, "height": 1600},
                "orientation": "square",
                "style_preset": "painterly",
                "abstraction": "surreal",
                "ai_disclosure": "This image was generated locally by software.",
            }
        )
        self.assertEqual(fields["type"], "download")
        self.assertIn("William Blake", fields["description"])


if __name__ == "__main__":
    unittest.main()
