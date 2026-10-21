import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from anskar.cli import main
from anskar.daily.errors import DailyError
from anskar.daily.notify import desktop_notification
from anskar.daily.rehearsal import (
    TEST_SERVICE,
    TEST_TIMER,
    cancel_test_cycle,
    cleanup_commands,
    finish_test_cycle,
    inhibit_work_command,
    launch_after_wake,
    notification_for,
    parse_foreign_inhibitors,
    render_test_units,
    run_inhibited_work,
    schedule_test_cycle,
    test_sleep_decision,
    wake_at,
    wake_conflicts,
    wait_for_resume_ready,
    write_report,
)


class _Runner:
    def __init__(self, fail_on: str | None = None):
        self.commands: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, command):
        self.commands.append(list(command))
        import subprocess

        code = 1 if self.fail_on and self.fail_on in command else 0
        return subprocess.CompletedProcess(command, code, "", "failed" if code else "")


class WakeScheduleTests(unittest.TestCase):
    def test_wake_is_three_minutes_ahead_and_separate_from_noon(self):
        now = datetime.fromisoformat("2026-10-21T15:00:00+02:00")
        when = wake_at(now)
        self.assertEqual(when - now, timedelta(minutes=3))
        service, timer = render_test_units(publish=True, when=when, python="/usr/bin/python3")
        self.assertIn("WakeSystem=true", timer)
        self.assertIn("OnCalendar=2026-10-21 15:03:00", timer)
        self.assertIn(f"Unit={TEST_SERVICE}", timer)
        self.assertNotIn("12:00:00", timer)
        self.assertNotIn("anskar-daily.timer", timer)
        self.assertNotIn("anskar-daily.service", service)
        self.assertIn("ANSKAR_TEST_PUBLISH=1", service)
        self.assertIn("--run", service)
        self.assertNotIn("daily-run", service)
        inhibited = " ".join(inhibit_work_command())
        self.assertIn("systemd-inhibit", inhibited)
        self.assertIn("--work", inhibited)
        self.assertIn("--finish", service)
        self.assertNotIn("RandomizedDelaySec", timer)

    def test_worker_command_inhibits_before_dropping_privileges(self):
        """Regression: runuser-before-inhibit exited in ~20ms and left status=scheduled."""
        with patch("anskar.daily.rehearsal.os.geteuid", return_value=0):
            with patch.dict(os.environ, {"ANSKAR_TEST_USER": "babuni", "ANSKAR_TEST_PUBLISH": "1"}):
                with patch("anskar.daily.rehearsal.pwd.getpwnam") as getpw:
                    getpw.return_value = type(
                        "Pw",
                        (),
                        {"pw_dir": "/home/babuni", "pw_uid": 1000},
                    )()
                    command = inhibit_work_command()
        self.assertEqual(command[0], "/usr/bin/systemd-inhibit")
        self.assertIn("runuser", command)
        self.assertLess(command.index("/usr/bin/systemd-inhibit"), command.index("runuser"))
        self.assertNotIn("--preserve-environment", command)
        self.assertTrue(any(part == "HOME=/home/babuni" for part in command))
        self.assertTrue(any(part == "ANSKAR_TEST_CYCLE=1" for part in command))
        self.assertIn("--work", command)

    def test_close_rtc_alarm_refuses_before_any_command(self):
        now = datetime.fromisoformat("2026-10-21T11:58:00+02:00")
        self.assertTrue(wake_conflicts(wake_at(now), datetime.fromisoformat("2026-10-21T12:00:00+02:00")))
        runner = _Runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(DailyError) as caught:
                schedule_test_cycle(
                    publish=False,
                    now=now,
                    runner=runner,
                    staging=root / "stage",
                    etc_dir=root / "etc",
                    production_next=datetime.fromisoformat("2026-10-21T12:00:00+02:00"),
                )
        self.assertEqual(caught.exception.kind, "busy")
        self.assertEqual(runner.commands, [])

    def test_schedule_writes_units_then_suspends_and_can_be_cleaned_up(self):
        now = datetime.fromisoformat("2026-10-21T15:00:00+02:00")
        runner = _Runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            noon = root / "anskar-daily.timer"
            noon.write_text("OnCalendar=*-*-* 12:00:00\nPersistent=false\n", encoding="utf-8")
            before = noon.read_text(encoding="utf-8")
            with patch.dict(os.environ, {"ANSKAR_TEST_CYCLE_DIR": str(root / "reports")}):
                report = schedule_test_cycle(
                    publish=True,
                    now=now,
                    runner=runner,
                    staging=root / "stage",
                    etc_dir=root / "etc",
                    production_next=datetime.fromisoformat("2026-10-21T12:00:00+02:00"),
                )
            self.assertEqual(noon.read_text(encoding="utf-8"), before)
            timer = (root / "stage" / TEST_TIMER).read_text(encoding="utf-8")
            self.assertIn("WakeSystem=true", timer)
            self.assertEqual(runner.commands[-1], ["systemctl", "suspend"])
            self.assertTrue(any(TEST_TIMER in command for command in runner.commands[-2]))
            self.assertNotIn("anskar-daily.timer", " ".join(" ".join(c) for c in runner.commands))
            self.assertTrue(report["armed"])
            saved = json.loads((root / "reports" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "scheduled")
            cleanup = _Runner()
            with patch.dict(os.environ, {"ANSKAR_TEST_CYCLE_DIR": str(root / "reports")}):
                cancelled = cancel_test_cycle(runner=cleanup)
            joined = " ".join(" ".join(command) for command in cleanup.commands)
            self.assertIn(TEST_TIMER, joined)
            self.assertNotIn("anskar-daily", joined)
            self.assertEqual(cancelled["noon_timer_changed"], False)
            self.assertTrue(all(command[0] == "sudo" for command in cleanup_commands()))

    def test_failed_install_does_not_suspend(self):
        runner = _Runner(fail_on="daemon-reload")
        now = datetime.fromisoformat("2026-10-21T15:00:00+02:00")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"ANSKAR_TEST_CYCLE_DIR": str(root / "reports")}):
                with self.assertRaises(DailyError):
                    schedule_test_cycle(
                        publish=False,
                        now=now,
                        runner=runner,
                        staging=root / "stage",
                        etc_dir=root / "etc",
                    )
        self.assertFalse(any(command == ["systemctl", "suspend"] for command in runner.commands))


class ResumeAndWorkTests(unittest.TestCase):
    def test_service_starts_only_after_the_system_is_ready(self):
        started: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            with patch("anskar.daily.rehearsal.wait_for_production_ready") as ready:
                ready.return_value = type(
                    "R",
                    (),
                    {"ok": True, "raise_if_failed": lambda self: None},
                )()
                with patch.dict(os.environ, {"ANSKAR_TEST_CYCLE": "1", "ANSKAR_TEST_PUBLISH": "1"}):
                    launch_after_wake(
                        sleep=lambda _seconds: None,
                        system_state=lambda: "degraded",
                        start_work=lambda: started.append("work"),
                        directory=Path(temporary),
                    )
                saved = json.loads((Path(temporary) / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(started, ["work"])
        ready.assert_called_once()
        self.assertEqual(ready.call_args.kwargs.get("settle_seconds"), 30)
        self.assertEqual(saved["stage"], "readiness")

    def test_worker_failure_is_not_left_as_scheduled(self):
        """The Oct 21 rehearsal ignored the worker exit and finished as scheduled."""
        import subprocess

        def failing_worker(command):
            self.assertEqual(command[0], "/usr/bin/systemd-inhibit")
            return subprocess.CompletedProcess(command, 1, "", "inhibit/runuser failed")

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_report(
                {
                    "status": "scheduled",
                    "stage": "scheduled",
                    "publish": True,
                    "success": False,
                    "scheduled_at": "2026-10-21T16:35:10+02:00",
                },
                directory=directory,
            )
            with patch("anskar.daily.rehearsal.wait_for_production_ready") as ready:
                ready.return_value = type(
                    "R",
                    (),
                    {"ok": True, "raise_if_failed": lambda self: None},
                )()
                with patch.dict(os.environ, {"ANSKAR_TEST_CYCLE": "1", "ANSKAR_TEST_PUBLISH": "1"}):
                    with patch("anskar.daily.rehearsal.inhibit_work_command", return_value=[
                        "/usr/bin/systemd-inhibit",
                        "runuser",
                        "-u",
                        "babuni",
                        "--",
                        "env",
                        "HOME=/home/babuni",
                        "python",
                        "-m",
                        "anskar",
                        "schedule",
                        "test-cycle",
                        "--work",
                    ]):
                        with self.assertRaises(DailyError) as caught:
                            launch_after_wake(
                                sleep=lambda _seconds: None,
                                directory=directory,
                                run_worker=failing_worker,
                            )
            saved = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
        self.assertNotEqual(saved["status"], "scheduled")
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["stage"], "failed")
        self.assertFalse(saved["success"])
        self.assertIn("exited 1", str(caught.exception))
        self.assertIsNone(saved.get("instagram_permalink") or None)

    def test_successful_worker_path_keeps_published_report(self):
        import subprocess

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def ok_worker(command):
                write_report(
                    {
                        "status": "completed",
                        "stage": "published",
                        "success": True,
                        "publish": True,
                        "title": "THE SICK ROSE",
                        "instagram_permalink": "https://www.instagram.com/p/rose/",
                        "etsy_status": "pending_shop_activation",
                        "instagram_status": "published",
                    },
                    directory=directory,
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("anskar.daily.rehearsal.wait_for_production_ready"):
                with patch.dict(os.environ, {"ANSKAR_TEST_CYCLE": "1", "ANSKAR_TEST_PUBLISH": "1"}):
                    with patch(
                        "anskar.daily.rehearsal.inhibit_work_command",
                        return_value=["/usr/bin/systemd-inhibit", "python", "--work"],
                    ):
                        launch_after_wake(
                            sleep=lambda _seconds: None,
                            directory=directory,
                            run_worker=ok_worker,
                        )
            saved = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["instagram_permalink"], "https://www.instagram.com/p/rose/")
        self.assertTrue(saved["success"])

    def test_unready_system_raises(self):
        with self.assertRaises(DailyError) as ctx:
            wait_for_resume_ready(
                sleep=lambda _seconds: None,
                system_state=lambda: "degraded",
                network_probe=lambda: False,
                ollama_probe=lambda: True,
                attempts=2,
                settle_seconds=0,
                pause_seconds=0,
                runner=lambda _cmd: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            )
        self.assertIn("network:", str(ctx.exception))

    def test_publish_simulation_notifies_and_does_not_publish_twice(self):
        calls: list[str] = []

        def work(*, publish: bool) -> dict:
            self.assertTrue(publish)
            calls.append("daily")
            return {
                "status": "completed",
                "success": True,
                "title": "THE SICK ROSE",
                "instagram_status": "published",
                "instagram_permalink": "https://www.instagram.com/p/rose/",
                "etsy_status": "pending_shop_activation",
                "run_id": "daily-test",
            }

        notes: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as temporary:
            report = run_inhibited_work(
                publish=True,
                work=work,
                notify=lambda **kwargs: notes.append((kwargs["title"], kwargs["body"])) or True,
                directory=Path(temporary),
            )
            again = json.loads((Path(temporary) / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(calls, ["daily"])
        self.assertEqual(report["instagram_permalink"], "https://www.instagram.com/p/rose/")
        self.assertEqual(notes[0][0], "THE SICK ROSE")
        self.assertIn("https://www.instagram.com/p/rose/", notes[0][1])
        self.assertIn("Published", notes[0][1])
        self.assertEqual(again["notification_sent"], True)
        self.assertEqual(again["etsy_status"], "pending_shop_activation")

    def test_diagnostic_mode_does_not_call_the_pipeline(self):
        from anskar.daily.rehearsal import perform_test_work

        with patch("anskar.daily.pipeline.daily_run", side_effect=AssertionError("pipeline")):
            report = perform_test_work(publish=False)
        from anskar.daily.rehearsal import perform_test_work as paint

        with patch("anskar.daily.pipeline.daily_run", return_value={
            "status": "completed",
            "id": "daily-once",
            "title": "LONDON",
            "instagram_status": "published",
            "instagram_permalink": "https://www.instagram.com/p/once/",
            "etsy_status": "pending_shop_activation",
            "poem_id": 4,
        }) as daily:
            painted = paint(publish=True)
        daily.assert_called_once()
        self.assertTrue(daily.call_args.kwargs["allow_test"])
        self.assertTrue(daily.call_args.kwargs["queue_etsy_only"])
        self.assertFalse(daily.call_args.kwargs["dry_run"])
        self.assertEqual(painted["instagram_permalink"], "https://www.instagram.com/p/once/")
        self.assertEqual(report["status"], "diagnostic")
        self.assertIsNone(report["instagram_permalink"])
        title, body = notification_for(report)
        self.assertIn("Nothing was published", body)
        self.assertTrue(title)

    def test_failure_is_kept_in_the_report(self):
        def work(*, publish: bool) -> dict:
            raise DailyError("sd-cli failed", kind="error")

        with tempfile.TemporaryDirectory() as temporary:
            report = run_inhibited_work(
                publish=True,
                work=work,
                notify=lambda **_kwargs: False,
                directory=Path(temporary),
            )
            saved = json.loads((Path(temporary) / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertIn("sd-cli failed", str(saved["error"]))
        self.assertFalse(saved["success"])


class SleepDecisionTests(unittest.TestCase):
    def test_inactive_user_may_suspend_only_after_a_successful_post(self):
        decision = test_sleep_decision(
            publish=True,
            success=True,
            idle_before=True,
            idle_after=True,
            foreign=[],
        )
        self.assertEqual(decision.action, "suspend")

    def test_active_user_stays_awake(self):
        decision = test_sleep_decision(
            publish=True,
            success=True,
            idle_before=True,
            idle_after=False,
            foreign=[],
        )
        self.assertEqual(decision.action, "stay_awake")

    def test_uncertain_idle_or_inhibitors_stay_awake(self):
        self.assertEqual(
            test_sleep_decision(
                publish=True, success=True, idle_before=True, idle_after=None, foreign=[]
            ).action,
            "stay_awake",
        )
        self.assertEqual(
            test_sleep_decision(
                publish=True, success=True, idle_before=True, idle_after=True, foreign=None
            ).action,
            "stay_awake",
        )
        self.assertEqual(
            test_sleep_decision(
                publish=True,
                success=True,
                idle_before=True,
                idle_after=True,
                foreign=["ModemManager sleep"],
            ).action,
            "stay_awake",
        )

    def test_failure_and_diagnostic_do_not_suspend(self):
        self.assertEqual(
            test_sleep_decision(
                publish=True, success=False, idle_before=True, idle_after=True, foreign=[]
            ).action,
            "stay_awake",
        )
        self.assertEqual(
            test_sleep_decision(
                publish=False, success=True, idle_before=True, idle_after=True, foreign=[]
            ).action,
            "stay_awake",
        )

    def test_finish_suspends_only_when_the_checks_agree(self):
        suspended: list[str] = []
        cleaned: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_report(
                {
                    "status": "completed",
                    "success": True,
                    "publish": True,
                    "title": "THE SICK ROSE",
                    "instagram_permalink": "https://www.instagram.com/p/rose/",
                },
                directory=directory,
            )
            decision = finish_test_cycle(
                sleep=lambda _seconds: None,
                idle=lambda: True,
                inhibitors=lambda: [],
                suspend_fn=lambda: suspended.append("now"),
                cleanup=lambda: cleaned.append("units"),
                grace_seconds=20,
                directory=directory,
                suspend_job_probe=lambda: True,
            )
            saved = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(decision.action, "suspend")
        self.assertEqual(suspended, ["now"])
        self.assertEqual(cleaned, ["units"])
        self.assertEqual(saved["sleep_action"], "suspend")
        self.assertTrue(saved["suspend_requested"])
        self.assertEqual(saved["suspend_result"], "requested")
        self.assertNotIn("anskar-daily", " ".join(cleanup_commands()[0]))

    def test_our_inhibitor_is_not_treated_as_foreign(self):
        text = "\n".join(
            [
                "WHO UID USER PID COMM WHAT WHY",
                "AnskarGrotzschnapf 1000 babuni 9 python sleep:idle:handle-lid-switch Daily artwork",
                "ModemManager 0 root 10 ModemManager sleep modem",
            ]
        )
        foreign = parse_foreign_inhibitors(text)
        self.assertEqual(len(foreign), 1)
        self.assertIn("ModemManager", foreign[0])

    def test_return_to_sleep_ignores_anskar_own_inhibitor(self):
        """Anskar's block must not prevent the post-publish suspend request."""
        suspended: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_report(
                {
                    "status": "completed",
                    "stage": "published",
                    "success": True,
                    "publish": True,
                    "instagram_permalink": "https://www.instagram.com/p/rose/",
                },
                directory=directory,
            )
            decision = finish_test_cycle(
                sleep=lambda _seconds: None,
                idle=lambda: True,
                inhibitors=lambda: parse_foreign_inhibitors(
                    "\n".join(
                        [
                            "WHO UID USER PID COMM WHAT WHY",
                            "AnskarGrotzschnapf 0 root 1 systemd-inhibit "
                            "idle:sleep:handle-lid-switch Daily artwork production",
                        ]
                    )
                ),
                suspend_fn=lambda: suspended.append("now"),
                cleanup=lambda: None,
                grace_seconds=0,
                directory=directory,
            )
            saved = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(decision.action, "suspend")
        self.assertEqual(suspended, ["now"])
        self.assertEqual(saved["suspend_result"], "requested")

    def test_failed_rehearsal_does_not_request_suspend(self):
        suspended: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_report(
                {
                    "status": "failed",
                    "stage": "failed",
                    "success": False,
                    "publish": True,
                    "error": "Worker exited 1",
                },
                directory=directory,
            )
            decision = finish_test_cycle(
                sleep=lambda _seconds: None,
                idle=lambda: True,
                inhibitors=lambda: [],
                suspend_fn=lambda: suspended.append("now"),
                cleanup=lambda: None,
                grace_seconds=0,
                directory=directory,
            )
            saved = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(decision.action, "stay_awake")
        self.assertEqual(suspended, [])
        self.assertEqual(saved["suspend_result"], "skipped")
        self.assertNotEqual(saved["status"], "scheduled")


class NotificationAndCliTests(unittest.TestCase):
    def test_missing_notify_send_does_not_fail_the_report(self):
        with patch.dict(os.environ, {"ANSKAR_NOTIFY": "1"}, clear=False):
            with patch("anskar.daily.notify.shutil.which", return_value=None):
                self.assertFalse(
                    desktop_notification(title="Anskar Grotzschnapf", body="Published")
                )

    def test_confirmation_is_required_before_arming(self):
        with patch("builtins.input", return_value="no"):
            with patch("anskar.cli.schedule_test_cycle") as schedule:
                code = main(["schedule", "test-cycle", "--publish"])
        self.assertEqual(code, 1)
        schedule.assert_not_called()

    def test_yes_arms_a_publishing_cycle(self):
        with patch("builtins.input", return_value="yes"):
            with patch("anskar.cli.schedule_test_cycle", return_value={
                "wake_at": "2026-10-21T15:03:00+02:00",
                "report": "data/test-cycle/latest.json",
            }) as schedule:
                code = main(["schedule", "test-cycle", "--publish"])
        self.assertEqual(code, 0)
        self.assertTrue(schedule.call_args.kwargs["publish"])

    def test_worker_refuses_to_run_outside_the_service(self):
        with patch.dict(os.environ, {"ANSKAR_TEST_CYCLE": ""}):
            self.assertEqual(main(["schedule", "test-cycle", "--work"]), 1)
