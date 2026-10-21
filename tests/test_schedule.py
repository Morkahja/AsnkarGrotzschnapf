import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from anskar.daily.ledger import swedish_calendar_day
from anskar.daily.power import (
    AfterRunDecision,
    decide_after_run,
    inhibit_command,
    verify_rtc_wake,
)
from anskar.daily.schedule import (
    NOON_CALENDAR,
    install_units,
    prepare_system_units,
    schedule_status,
    unit_source_dir,
)


class ScheduleUnitTests(unittest.TestCase):
    def test_repo_timer_is_noon_without_persistent_catchup(self):
        timer = (unit_source_dir() / "anskar-daily.timer").read_text(encoding="utf-8")
        self.assertIn("12:00:00", timer)
        self.assertIn("Persistent=false", timer)
        self.assertIn("RandomizedDelaySec=0", timer)
        self.assertNotIn("06:30", timer)
        self.assertNotIn("Persistent=true", timer)

    def test_system_timer_wakes_from_sleep(self):
        timer = (unit_source_dir() / "system" / "anskar-daily.timer").read_text(encoding="utf-8")
        self.assertIn("WakeSystem=true", timer)
        self.assertIn("12:00:00", timer)
        self.assertIn("Persistent=false", timer)
        self.assertIn("RandomizedDelaySec=0", timer)

    def test_services_inhibit_sleep_during_production(self):
        user = (unit_source_dir() / "anskar-daily.service").read_text(encoding="utf-8")
        system = (unit_source_dir() / "system" / "anskar-daily.service").read_text(encoding="utf-8")
        self.assertIn("systemd-inhibit", user)
        self.assertIn("systemd-inhibit", system)
        self.assertIn("User=@USER@", system)
        self.assertNotIn("User=root", system)
        cmd = inhibit_command("/tmp/python", "-m", "anskar", "daily-run")
        self.assertEqual(cmd[0], "/usr/bin/systemd-inhibit")
        self.assertIn("--mode=block", cmd)
        self.assertIn("idle:sleep:handle-lid-switch", " ".join(cmd))

    def test_install_refuses_old_0630_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "systemd"
            source.mkdir()
            (source / "anskar-daily.service").write_text("ExecStart=@PYTHON@ -m anskar daily-run\n")
            (source / "anskar-daily.timer").write_text(
                "[Timer]\nOnCalendar=*-*-* 06:30:00\nPersistent=true\n"
            )
            with patch("anskar.daily.schedule.unit_source_dir", return_value=source):
                with patch.dict(os.environ, {"ANSKAR_SYSTEMD_USER_DIR": str(root / "user")}):
                    with self.assertRaises(Exception) as caught:
                        install_units(enable=False)
            message = str(caught.exception).lower()
            self.assertTrue("12:00" in message or "06:30" in message or "persistent" in message)

    def test_install_writes_noon_units_without_enabling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit_dir = root / "user-units"
            with patch.dict(os.environ, {"ANSKAR_SYSTEMD_USER_DIR": str(unit_dir)}):
                with patch("anskar.daily.schedule.shutil.which", return_value=None):
                    report = install_units(enable=False)
            text = (unit_dir / "anskar-daily.timer").read_text(encoding="utf-8")
            self.assertIn("12:00:00", text)
            self.assertIn("Persistent=false", text)
            service = (unit_dir / "anskar-daily.service").read_text(encoding="utf-8")
            self.assertIn("systemd-inhibit", service)
            self.assertFalse(report["enabled_now"])

    def test_prepare_system_stages_without_enabling(self):
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary) / "staging"
            with patch.dict(os.environ, {"ANSKAR_SYSTEMD_SYSTEM_STAGING": str(staging)}):
                report = prepare_system_units()
            self.assertTrue(report["prepared"])
            self.assertFalse(report["enabled"])
            timer = (staging / "anskar-daily.timer").read_text(encoding="utf-8")
            self.assertIn("WakeSystem=true", timer)
            service = (staging / "anskar-daily.service").read_text(encoding="utf-8")
            self.assertIn("User=", service)
            self.assertNotIn("@USER@", service)
            self.assertTrue(any("disable --now anskar-daily.timer" in c for c in report["activation_commands"]))
            self.assertTrue(any("sudo" in c for c in report["activation_commands"]))


class PowerPolicyTests(unittest.TestCase):
    def test_already_awake_manual_run_stays_awake(self):
        decision = decide_after_run(scheduled=False, idle=True)
        self.assertEqual(decision.action, "stay_awake")
        self.assertFalse(decision.force_suspend)

    def test_user_active_after_wake_never_suspends(self):
        decision = decide_after_run(scheduled=True, idle=False)
        self.assertEqual(decision.action, "stay_awake")
        self.assertFalse(decision.force_suspend)

    def test_scheduled_idle_leaves_to_kde_without_forcing(self):
        decision = decide_after_run(scheduled=True, idle=True)
        self.assertEqual(decision.action, "leave_to_idle_manager")
        self.assertFalse(decision.force_suspend)

    def test_unknown_idle_stays_awake(self):
        decision = decide_after_run(scheduled=True, idle=None)
        self.assertEqual(decision.action, "stay_awake")
        self.assertFalse(decision.force_suspend)

    def test_verify_rtc_wake_does_not_suspend(self):
        report = verify_rtc_wake()
        self.assertFalse(report["suspended"])
        self.assertFalse(report["published"])
        self.assertIn("wakealarm_path", report)


class SwedishDayTests(unittest.TestCase):
    def test_swedish_day_uses_stockholm_timezone(self):
        # 23:30 UTC on 20 Oct is already 01:30 on 21 Oct in Stockholm (CEST).
        moment = datetime(2026, 10, 20, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(swedish_calendar_day(moment), "2026-10-21")
        local = moment.astimezone(ZoneInfo("Europe/Stockholm"))
        self.assertEqual(local.strftime("%Y-%m-%d"), "2026-10-21")

    def test_noon_calendar_constant(self):
        self.assertEqual(NOON_CALENDAR, "*-*-* 12:00:00")


class DuplicateDayTests(unittest.TestCase):
    def test_second_live_run_same_swedish_day_blocked(self):
        # Reuse daily pipeline fixture via a focused ledger check.
        import json
        import tempfile
        from datetime import date
        from unittest.mock import patch

        from anskar.archive.importer import import_collection
        from anskar.daily.errors import DailyError
        from anskar.daily.pipeline import daily_run
        from anskar.db import connect
        from anskar.etsy.state import PENDING_VERIFICATION, set_etsy_state
        from anskar.artwork.generator import ArtworkRecord
        from anskar.archive.selection import select_poem

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        archive = root / "archive.sqlite"
        daily_db = root / "daily.sqlite"
        packs = root / "packs"
        artworks = root / "artworks"
        packs.mkdir()
        artworks.mkdir()
        set_etsy_state(PENDING_VERIFICATION, path=root / "etsy-state.json")
        collection = root / "poems.json"
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
                            "body": "Little lamb\n",
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
                            "body": "Tiger tiger\n",
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
        with connect(archive) as connection:
            import_collection(connection, collection, on=date.today())

        def fake_artwork(database: Path, *, seed: int = 7) -> ArtworkRecord:
            with connect(database) as connection:
                selection = select_poem(connection)
            image = artworks / f"poem-{selection.poem.id}-seed-{seed}.png"
            image.write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x08\x00\x00\x00\x08"
                b"\x08\x02\x00\x00\x00K\xd3\x9b\x81\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            meta = {
                "experiment": "production",
                "poem_id": selection.poem.id,
                "selection_id": selection.id,
                "title": selection.poem.title,
                "author": selection.poem.author,
                "source_text": selection.poem.body,
                "seed": seed,
                "width": 1600,
                "height": 1600,
                "model_id": "z-image-turbo",
                "prompt_sha256": "a" * 64,
                "output_path": str(image),
            }
            image.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
            return ArtworkRecord(
                id=seed,
                poem_id=selection.poem.id,
                selection_id=selection.id,
                title=selection.poem.title,
                author=selection.poem.author,
                prompt_sha256="a" * 64,
                model="z-image-turbo",
                seed=seed,
                width=1600,
                height=1600,
                duration_seconds=1.0,
                settings_json="{}",
                output_path=str(image),
                status="succeeded",
                error=None,
            )

        def fake_pack(metadata: Path, *, suffix: str = "a") -> dict:
            record = json.loads(metadata.read_text(encoding="utf-8"))
            pack_id = f"production-poem-{record['poem_id']}-seed-{record['seed']}-{suffix}"
            folder = packs / pack_id
            (folder / "etsy").mkdir(parents=True)
            (folder / "instagram").mkdir(parents=True)
            (folder / "print").mkdir(parents=True)
            png = metadata.with_suffix(".png").read_bytes()
            (folder / "etsy" / "print.png").write_bytes(png)
            (folder / "instagram" / "preview.png").write_bytes(png)
            (folder / "print" / "artwork.png").write_bytes(png)
            manifest = {
                "artwork_id": pack_id,
                "publication_ready": True,
                "rights": {
                    "matched": True,
                    "publication_ready": True,
                    "title": record["title"],
                    "author": record["author"],
                    "poem_id": record["poem_id"],
                },
            }
            (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            return manifest

        lock = root / "daily.lock"
        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            first = daily_run(
                archive_db=archive,
                daily_db=daily_db,
                packs=packs,
                artworks=artworks,
                generate=lambda *a, **k: fake_artwork(archive),
                prepare_pack=lambda metadata, **k: fake_pack(metadata),
                publish_instagram=lambda pack_id, **k: {
                    "published": True,
                    "media_id": "ig-1",
                    "permalink": "https://www.instagram.com/p/a/",
                    "pack_id": pack_id,
                },
                lock=lock,
            )
        self.assertEqual(first["status"], "completed")
        with patch("anskar.daily.pipeline.etsy_is_ready", return_value=False):
            with self.assertRaises(DailyError) as caught:
                daily_run(
                    archive_db=archive,
                    daily_db=daily_db,
                    packs=packs,
                    artworks=artworks,
                    generate=lambda *a, **k: (_ for _ in ()).throw(AssertionError("dup")),
                    prepare_pack=lambda *a, **k: (_ for _ in ()).throw(AssertionError("dup")),
                    publish_instagram=lambda *a, **k: (_ for _ in ()).throw(AssertionError("dup")),
                    lock=lock,
                )
        self.assertEqual(caught.exception.kind, "busy")
        self.assertIn("Swedish local day", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
