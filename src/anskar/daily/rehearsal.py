"""One-time wake, paint, publish, and optional return-to-sleep rehearsal.

This does not replace the noon timer. Temporary units are named
anskar-test-cycle.* and are removed after the service stops.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from anskar.config import PROJECT_ROOT
from anskar.daily.errors import DailyError
from anskar.daily.notify import desktop_notification
from anskar.daily.power import inhibit_argv, inhibit_command
from anskar.daily.readiness import (
    READY_ATTEMPTS,
    READY_PAUSE_SECONDS,
    SETTLE_SECONDS,
    wait_for_production_ready,
)
from anskar.daily.schedule import default_python, system_staging_dir

TEST_SERVICE = "anskar-test-cycle.service"
TEST_TIMER = "anskar-test-cycle.timer"
WAKE_LEAD = timedelta(minutes=3)
CONFLICT_WINDOW = timedelta(minutes=15)
SLEEP_GRACE_SECONDS = 20
REPORT_NAME = "latest.json"
# Artwork generation plus Instagram can take well over two minutes.
WORK_TIMEOUT_SECONDS = 3 * 60 * 60
SUBPROCESS_TIMEOUT_SECONDS = 120

PUBLISH_WARNING = (
    "This will suspend the computer, wake it about 3 minutes later, "
    "paint one new artwork, and publish that artwork to @anskargrotzschnapf. "
    "Etsy is only queued. The noon schedule is not changed."
)
DIAGNOSTIC_WARNING = (
    "This will suspend the computer and wake it about 3 minutes later "
    "so the test service can run. No artwork is generated and nothing is published. "
    "The noon schedule is not changed."
)
CONFIRM_PROMPT = "Type yes to continue: "


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class TestSleepDecision:
    action: str  # stay_awake | suspend
    reason: str


def report_directory() -> Path:
    override = os.environ.get("ANSKAR_TEST_CYCLE_DIR")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / "data" / "test-cycle"


def warning_text(*, publish: bool) -> str:
    return PUBLISH_WARNING if publish else DIAGNOSTIC_WARNING


def wake_at(now: datetime, *, lead: timedelta = WAKE_LEAD) -> datetime:
    if now.tzinfo is None:
        now = now.astimezone()
    return now + lead


def wake_conflicts(when: datetime, existing: datetime | None) -> bool:
    """True when another RTC alarm is close enough to steal or be stolen."""
    if existing is None:
        return False
    if existing.tzinfo is None:
        existing = existing.replace(tzinfo=when.tzinfo)
    return abs(existing - when) < CONFLICT_WINDOW


def render_test_units(
    *,
    publish: bool,
    when: datetime,
    python: str | None = None,
) -> tuple[str, str]:
    """Return (service_text, timer_text). The noon unit names are not used."""
    program = python or default_python()
    publish_flag = "1" if publish else "0"
    service = "\n".join(
        [
            "[Unit]",
            "Description=One-time Anskar wake rehearsal (not the noon schedule)",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
            f"WorkingDirectory={PROJECT_ROOT}",
            f"Environment=HOME={Path.home()}",
            f"Environment=USER={os.environ.get('USER', '')}",
            f"Environment=PYTHONPATH={PROJECT_ROOT / 'src'}",
            f"Environment=XDG_RUNTIME_DIR=/run/user/{os.getuid()}",
            f"Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{os.getuid()}/bus",
            "Environment=ANSKAR_TEST_CYCLE=1",
            f"Environment=ANSKAR_TEST_PUBLISH={publish_flag}",
            f"Environment=ANSKAR_TEST_USER={os.environ.get('USER', '')}",
            f"Environment=ANSKAR_TEST_UID={os.getuid()}",
            "# Runs as root so cleanup and suspend need no password. Painting drops to ANSKAR_TEST_USER.",
            "# The command waits out the wake transition before it takes a sleep inhibitor.",
            f"ExecStart={program} -m anskar schedule test-cycle --run",
            f"ExecStopPost=-{program} -m anskar schedule test-cycle --finish",
            "",
        ]
    )
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    timer = "\n".join(
        [
            "[Unit]",
            "Description=One-time RTC wake for the Anskar rehearsal",
            "",
            "[Timer]",
            f"OnCalendar={stamp}",
            "AccuracySec=1s",
            "Persistent=false",
            "WakeSystem=true",
            f"Unit={TEST_SERVICE}",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return service, timer


def wait_for_resume_ready(
    *,
    sleep: Callable[[float], None],
    system_state: Callable[[], str] | None = None,
    session_bus: Path | None = None,
    settle_seconds: float = SETTLE_SECONDS,
    attempts: int = READY_ATTEMPTS,
    pause_seconds: float = READY_PAUSE_SECONDS,
    runner=None,
    network_probe: Callable[[], bool] | None = None,
    ollama_probe: Callable[[], bool] | None = None,
    start_ollama: Callable[[], None] | None = None,
    cpu_root: Path | None = None,
    project_root: Path | None = None,
    python_path: str | None = None,
) -> None:
    """
    Wait until Anskar's production dependencies are ready after wake.

    session_bus is ignored: a missing user bus or a degraded systemd state from
    an unrelated unit must not block painting when real dependencies work.
    """
    del session_bus  # retained for call-site compatibility
    kwargs: dict[str, object] = {
        "sleep": sleep,
        "runner": runner,
        "settle_seconds": settle_seconds,
        "attempts": attempts,
        "pause_seconds": pause_seconds,
        "system_state": system_state,
        "network_probe": network_probe,
        "ollama_probe": ollama_probe,
        "start_ollama": start_ollama,
        "project_root": project_root,
        "python_path": python_path,
    }
    if cpu_root is not None:
        kwargs["cpu_root"] = cpu_root
    wait_for_production_ready(**kwargs)  # type: ignore[arg-type]


def parse_foreign_inhibitors(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    body = lines[1:] if lines[0].upper().startswith("WHO") else lines
    foreign: list[str] = []
    for line in body:
        if "AnskarGrotzschnapf" in line:
            continue
        if "sleep" in line.lower():
            foreign.append(line)
    return foreign


def test_sleep_decision(
    *,
    publish: bool,
    success: bool,
    idle_before: bool | None,
    idle_after: bool | None,
    foreign: list[str] | None,
) -> TestSleepDecision:
    if not publish or not success:
        return TestSleepDecision(
            "stay_awake",
            "The rehearsal did not publish successfully. Leaving the computer awake.",
        )
    if idle_before is not True or idle_after is not True:
        return TestSleepDecision(
            "stay_awake",
            "The user is active or idle status is uncertain. Leaving the computer awake.",
        )
    if foreign is None:
        return TestSleepDecision(
            "stay_awake",
            "Other sleep inhibitors could not be checked. Leaving the computer awake.",
        )
    if foreign:
        return TestSleepDecision(
            "stay_awake",
            "Another sleep inhibitor is held. Leaving the computer awake.",
        )
    return TestSleepDecision(
        "suspend",
        "The session stayed idle and no other sleep inhibitor is held.",
    )


def write_report(payload: dict[str, object], *, directory: Path | None = None) -> Path:
    folder = directory or report_directory()
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    latest = folder / REPORT_NAME
    latest.write_text(text, encoding="utf-8")
    stamp = str(payload.get("finished_at") or payload.get("scheduled_at") or "report")
    safe = "".join(ch if ch.isalnum() else "-" for ch in stamp)[:40]
    (folder / f"{safe}.json").write_text(text, encoding="utf-8")
    return latest


def update_report(
    updates: dict[str, object],
    *,
    directory: Path | None = None,
) -> dict[str, object]:
    """Merge fields into the persistent rehearsal report without dropping history."""
    folder = directory or report_directory()
    path = folder / REPORT_NAME
    current: dict[str, object] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, json.JSONDecodeError):
            current = {}
    current.update(updates)
    write_report(current, directory=directory)
    return current


def perform_test_work(*, publish: bool) -> dict[str, object]:
    """Run the production pipeline once, or record a diagnostic wake."""
    if not publish:
        return {
            "status": "diagnostic",
            "stage": "completed",
            "success": True,
            "title": "Wake test",
            "author": None,
            "poem_id": None,
            "instagram_status": "not_run",
            "instagram_permalink": None,
            "etsy_status": None,
            "error": None,
            "run_id": None,
        }
    from anskar.daily.pipeline import daily_run

    try:
        result = daily_run(dry_run=False, allow_test=True, queue_etsy_only=True)
    except Exception as exc:  # noqa: BLE001 — the report has to survive a failed paint
        return {
            "status": "failed",
            "stage": "failed",
            "success": False,
            "title": None,
            "author": None,
            "poem_id": None,
            "instagram_status": "failed",
            "instagram_permalink": None,
            "etsy_status": None,
            "error": str(exc),
            "run_id": None,
        }
    success = result.get("status") == "completed" and result.get("instagram_status") == "published"
    return {
        "status": "completed" if success else "failed",
        "stage": "published" if success else "failed",
        "success": success,
        "title": result.get("title"),
        "author": result.get("author"),
        "poem_id": result.get("poem_id"),
        "instagram_status": result.get("instagram_status"),
        "instagram_permalink": result.get("instagram_permalink"),
        "etsy_status": result.get("etsy_status"),
        "error": None if success else str(result.get("error_message") or result.get("status")),
        "run_id": result.get("id"),
    }


def notification_for(report: dict[str, object]) -> tuple[str, str]:
    title = str(report.get("title") or "Anskar Grotzschnapf")
    if report.get("success") and report.get("instagram_permalink"):
        body = f"Published\n{report['instagram_permalink']}"
    elif report.get("status") == "diagnostic":
        body = "The test service ran. Nothing was published."
    else:
        body = f"Publication failed\n{report.get('error') or 'See the local report.'}"
    return title, body


def worker_environment(user: str) -> dict[str, str]:
    """
    Environment for the dropped-privilege paint process.

    Credentials live under the artist's HOME. Never inherit root's HOME via
    runuser --preserve-environment. Values here are not secrets.
    """
    info = pwd.getpwnam(user)
    uid = str(info.pw_uid)
    runtime = f"/run/user/{uid}"
    env = {
        "HOME": info.pw_dir,
        "USER": user,
        "LOGNAME": user,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONPATH": os.environ.get("PYTHONPATH") or str(PROJECT_ROOT / "src"),
        "ANSKAR_TEST_CYCLE": "1",
        "ANSKAR_TEST_PUBLISH": os.environ.get("ANSKAR_TEST_PUBLISH", "0"),
        "ANSKAR_TEST_USER": user,
        "ANSKAR_TEST_UID": uid,
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
    }
    override = os.environ.get("ANSKAR_TEST_CYCLE_DIR")
    if override:
        env["ANSKAR_TEST_CYCLE_DIR"] = override
    return env


def inhibit_work_command() -> list[str]:
    """
    Hold the sleep inhibitor as root, then drop to ANSKAR_TEST_USER for painting.

    The previous order (runuser → systemd-inhibit) exited in milliseconds after
    wake without starting Python, left the report at status=scheduled, and still
    returned success to systemd. Inhibit must wrap runuser, not the reverse.
    """
    python = default_python()
    work = [python, "-m", "anskar", "schedule", "test-cycle", "--work"]
    user = os.environ.get("ANSKAR_TEST_USER") or ""
    if os.geteuid() == 0 and user and user != "root":
        env = worker_environment(user)
        dropped = [
            "runuser",
            "-u",
            user,
            "--",
            "env",
            *[f"{key}={value}" for key, value in env.items()],
            *work,
        ]
        return inhibit_argv(dropped)
    return inhibit_command(python, "-m", "anskar", "schedule", "test-cycle", "--work")


def schedule_test_cycle(
    *,
    publish: bool,
    now: datetime | None = None,
    runner: Runner | None = None,
    staging: Path | None = None,
    etc_dir: Path | None = None,
    production_next: datetime | None = None,
    suspend: bool = True,
) -> dict[str, object]:
    """Install the one-time system timer and, when suspend is true, suspend the PC."""
    moment = now or datetime.now().astimezone()
    when = wake_at(moment)
    if wake_conflicts(when, production_next):
        raise DailyError(
            "The rehearsal wake is too close to another RTC alarm. "
            "The noon timer was not changed.",
            kind="busy",
        )
    service_text, timer_text = render_test_units(publish=publish, when=when)
    stage = staging or (system_staging_dir() / "test-cycle")
    stage.mkdir(parents=True, exist_ok=True)
    service_path = stage / TEST_SERVICE
    timer_path = stage / TEST_TIMER
    service_path.write_text(service_text, encoding="utf-8")
    timer_path.write_text(timer_text, encoding="utf-8")
    destination = etc_dir or Path("/etc/systemd/system")
    commands = install_commands(service_path, timer_path, destination)
    if suspend:
        commands.append(["systemctl", "suspend"])
    scheduled = {
        "status": "scheduled",
        "stage": "scheduled",
        "publish": publish,
        "scheduled_at": moment.isoformat(timespec="seconds"),
        "wake_at": when.isoformat(timespec="seconds"),
        "service": TEST_SERVICE,
        "timer": TEST_TIMER,
        "noon_timer_changed": False,
        "success": False,
        "error": None,
        "instagram_permalink": None,
        "report": str(report_directory() / REPORT_NAME),
    }
    write_report(scheduled)
    run = runner or _subprocess
    for command in commands:
        completed = run(command)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise DailyError(
                f"Could not arm the rehearsal ({' '.join(command)}). {detail}".strip(),
                kind="error",
            )
    scheduled["armed"] = True
    scheduled["commands"] = [" ".join(command) for command in commands]
    return scheduled


def install_commands(service_path: Path, timer_path: Path, destination: Path) -> list[list[str]]:
    return [
        ["sudo", "cp", str(service_path), str(destination / TEST_SERVICE)],
        ["sudo", "cp", str(timer_path), str(destination / TEST_TIMER)],
        ["sudo", "systemctl", "daemon-reload"],
        ["sudo", "systemctl", "enable", "--now", TEST_TIMER],
    ]


def cleanup_commands() -> list[list[str]]:
    """Remove only the rehearsal units. The noon timer is not named here."""
    return [
        ["sudo", "systemctl", "disable", "--now", TEST_TIMER],
        ["sudo", "rm", "-f", f"/etc/systemd/system/{TEST_SERVICE}", f"/etc/systemd/system/{TEST_TIMER}"],
        ["sudo", "systemctl", "daemon-reload"],
    ]


def remove_test_units(*, runner: Runner | None = None) -> list[str]:
    run = runner or _subprocess
    errors: list[str] = []
    for command in cleanup_commands():
        completed = run(command)
        if completed.returncode != 0:
            errors.append((completed.stderr or completed.stdout or "failed").strip())
    return errors


def cancel_test_cycle(*, runner: Runner | None = None) -> dict[str, object]:
    errors = remove_test_units(runner=runner)
    report = {
        "status": "cancelled" if not errors else "cancel_failed",
        "noon_timer_changed": False,
        "errors": errors,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    write_report(report)
    return report


def launch_after_wake(
    *,
    sleep: Callable[[float], None] | None = None,
    system_state: Callable[[], str] | None = None,
    session_bus: Path | None = None,
    start_work: Callable[[], None] | None = None,
    network_probe: Callable[[], bool] | None = None,
    ollama_probe: Callable[[], bool] | None = None,
    start_ollama: Callable[[], None] | None = None,
    cpu_root: Path | None = None,
    project_root: Path | None = None,
    python_path: str | None = None,
    runner=None,
    directory: Path | None = None,
    run_worker: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Wait until production dependencies are ready, then start the inhibited paint."""
    if os.environ.get("ANSKAR_TEST_CYCLE") != "1" and start_work is None:
        raise DailyError(
            "The wake service was not started by the rehearsal unit.",
            kind="error",
        )
    publish = os.environ.get("ANSKAR_TEST_PUBLISH") == "1"
    report_dir = directory or report_directory()
    update_report(
        {
            "status": "resumed",
            "stage": "resumed",
            "publish": publish,
            "resumed_at": _now_iso(),
            "success": False,
            "error": None,
            "instagram_permalink": None,
        },
        directory=report_dir,
    )
    pause = sleep or _sleep
    try:
        wait_for_resume_ready(
            sleep=pause,
            system_state=system_state,
            session_bus=session_bus,
            network_probe=network_probe,
            ollama_probe=ollama_probe,
            start_ollama=start_ollama,
            cpu_root=cpu_root,
            project_root=project_root,
            python_path=python_path,
            runner=runner,
        )
    except DailyError as exc:
        update_report(
            {
                "status": "failed",
                "stage": "readiness",
                "success": False,
                "publish": publish,
                "error": str(exc),
                "finished_at": _now_iso(),
                "instagram_permalink": None,
            },
            directory=report_dir,
        )
        raise
    update_report(
        {"status": "readiness", "stage": "readiness", "publish": publish},
        directory=report_dir,
    )
    if start_work is not None:
        start_work()
        return
    update_report(
        {"status": "generating", "stage": "generating", "publish": publish},
        directory=report_dir,
    )
    command = inhibit_work_command()
    print("Starting rehearsal worker (inhibit wraps runuser).", flush=True)
    print(f"Worker argv: {_safe_argv(command)}", flush=True)
    worker = run_worker or _run_worker
    try:
        completed = worker(command)
    except subprocess.TimeoutExpired as exc:
        update_report(
            {
                "status": "failed",
                "stage": "generating",
                "success": False,
                "publish": publish,
                "error": f"Worker timed out after {WORK_TIMEOUT_SECONDS}s.",
                "finished_at": _now_iso(),
                "worker_returncode": None,
            },
            directory=report_dir,
        )
        raise DailyError(
            f"The rehearsal worker timed out after {WORK_TIMEOUT_SECONDS}s.",
            kind="error",
        ) from exc
    except OSError as exc:
        update_report(
            {
                "status": "failed",
                "stage": "generating",
                "success": False,
                "publish": publish,
                "error": f"Could not start the rehearsal worker: {exc}",
                "finished_at": _now_iso(),
            },
            directory=report_dir,
        )
        raise DailyError(f"Could not start the rehearsal worker: {exc}", kind="error") from exc

    payload = _read_report(report_dir)
    if completed.returncode != 0:
        detail = _worker_failure_detail(completed, payload)
        if payload.get("status") in {
            None,
            "scheduled",
            "resumed",
            "readiness",
            "generating",
        }:
            update_report(
                {
                    "status": "failed",
                    "stage": "failed",
                    "success": False,
                    "publish": publish,
                    "error": detail,
                    "finished_at": _now_iso(),
                    "worker_returncode": completed.returncode,
                    "instagram_permalink": payload.get("instagram_permalink"),
                },
                directory=report_dir,
            )
        else:
            update_report(
                {
                    "worker_returncode": completed.returncode,
                    "error": payload.get("error") or detail,
                },
                directory=report_dir,
            )
        print(detail, file=sys.stderr, flush=True)
        raise DailyError(detail, kind="error")
    if not payload.get("success"):
        detail = str(payload.get("error") or "Worker exited 0 without a successful report.")
        update_report(
            {
                "status": "failed",
                "stage": str(payload.get("stage") or "failed"),
                "success": False,
                "error": detail,
                "finished_at": _now_iso(),
                "worker_returncode": completed.returncode,
            },
            directory=report_dir,
        )
        raise DailyError(detail, kind="error")


def run_inhibited_work(
    *,
    publish: bool,
    work: Callable[..., dict[str, object]] | None = None,
    notify: Callable[..., bool] | None = None,
    directory: Path | None = None,
) -> dict[str, object]:
    """Paint and publish while the sleep inhibitor is already held."""
    worker = work or perform_test_work
    update_report(
        {
            "status": "generating",
            "stage": "generating",
            "publish": publish,
            "success": False,
            "error": None,
        },
        directory=directory,
    )
    try:
        if publish:
            update_report(
                {"stage": "publishing", "status": "publishing"},
                directory=directory,
            )
        outcome = worker(publish=publish)
    except Exception as exc:  # noqa: BLE001
        outcome = {
            "status": "failed",
            "stage": "failed",
            "success": False,
            "title": None,
            "instagram_permalink": None,
            "error": str(exc),
        }
    success = bool(outcome.get("success"))
    if success and publish:
        status = "completed"
        stage_after = "published"
    elif success:
        status = "diagnostic"
        stage_after = "completed"
    else:
        status = "failed"
        stage_after = "failed"
    report = update_report(
        {
            **outcome,
            "status": status,
            "stage": "notifying",
            "success": success,
            "publish": publish,
            "noon_timer_changed": False,
            "finished_at": _now_iso(),
        },
        directory=directory,
    )
    report["notification_sent"] = _deliver(report, notify)
    report["stage"] = stage_after
    report["finished_at"] = _now_iso()
    write_report(report, directory=directory)
    return report


def finish_test_cycle(
    *,
    sleep: Callable[[float], None] | None = None,
    idle: Callable[[], bool | None] | None = None,
    inhibitors: Callable[[], list[str] | None] | None = None,
    suspend_fn: Callable[[], None] | None = None,
    cleanup: Callable[[], None] | None = None,
    grace_seconds: float = SLEEP_GRACE_SECONDS,
    directory: Path | None = None,
    suspend_job_probe: Callable[[], bool | None] | None = None,
) -> TestSleepDecision:
    """After the paint process has exited, clean up and maybe suspend."""
    payload = _read_report(directory)
    publish = bool(payload.get("publish"))
    success = bool(payload.get("success"))
    pause = sleep or _sleep
    idle_fn = idle or _idle
    foreign_fn = inhibitors or _foreign
    # The worker's systemd-inhibit process must already have exited. Anskar's
    # own who-line is never treated as a foreign blocker.
    before = idle_fn()
    pause(grace_seconds)
    after = idle_fn()
    try:
        foreign = foreign_fn()
    except (OSError, subprocess.TimeoutExpired):
        foreign = None
    decision = test_sleep_decision(
        publish=publish,
        success=success,
        idle_before=before,
        idle_after=after,
        foreign=foreign,
    )
    payload["sleep_action"] = decision.action
    payload["sleep_reason"] = decision.reason
    payload["stage"] = "sleeping" if decision.action == "suspend" else "completed"
    if decision.action != "suspend":
        payload["suspend_result"] = "skipped"
        payload["suspend_requested"] = False
        print(decision.reason, flush=True)
    write_report(payload, directory=directory)
    if cleanup is not None:
        cleanup()
    else:
        payload["cleanup_errors"] = remove_test_units()
        write_report(payload, directory=directory)
    if decision.action == "suspend":
        _request_suspend(
            payload,
            directory=directory,
            suspend_fn=suspend_fn,
            suspend_job_probe=suspend_job_probe,
        )
    elif success:
        payload["status"] = payload.get("status") or "completed"
        payload["stage"] = "completed"
        write_report(payload, directory=directory)
    return decision


def _request_suspend(
    payload: dict[str, object],
    *,
    directory: Path | None,
    suspend_fn: Callable[[], None] | None,
    suspend_job_probe: Callable[[], bool | None] | None,
) -> None:
    """Ask systemd to suspend. Distinguish a request from a queued sleep job."""
    print("Requesting deep sleep via systemctl suspend.", flush=True)
    payload["suspend_requested"] = True
    payload["stage"] = "sleeping"
    write_report(payload, directory=directory)
    try:
        if suspend_fn is not None:
            suspend_fn()
            payload["suspend_result"] = "requested"
            print("Suspend callback returned (test harness).", flush=True)
        else:
            completed = _subprocess(["systemctl", "suspend"])
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip() or (
                    f"systemctl suspend exited {completed.returncode}"
                )
                payload["suspend_result"] = "failed"
                payload["suspend_error"] = detail
                payload["stage"] = "completed"
                payload["sleep_action"] = "stay_awake"
                payload["sleep_reason"] = f"Suspend request failed: {detail}"
                print(payload["sleep_reason"], file=sys.stderr, flush=True)
                write_report(payload, directory=directory)
                return
            payload["suspend_result"] = "requested"
            print(
                "systemctl suspend accepted the request (not yet a confirmed sleep).",
                flush=True,
            )
            probe = suspend_job_probe or _suspend_job_queued
            queued = probe()
            if queued is True:
                payload["suspend_result"] = "confirmed_queued"
                print("Suspend target is queued in systemd jobs.", flush=True)
            elif queued is False:
                print(
                    "Suspend was requested; no sleep job visible yet "
                    "(transition may still proceed).",
                    flush=True,
                )
    except (OSError, subprocess.TimeoutExpired) as exc:
        payload["suspend_result"] = "failed"
        payload["suspend_error"] = str(exc)
        payload["stage"] = "completed"
        payload["sleep_action"] = "stay_awake"
        payload["sleep_reason"] = f"Suspend request failed: {exc}"
        print(payload["sleep_reason"], file=sys.stderr, flush=True)
    payload["finished_at"] = _now_iso()
    if payload.get("suspend_result") in {"requested", "confirmed_queued"}:
        payload["stage"] = "completed"
        payload["status"] = "completed"
    write_report(payload, directory=directory)


def _deliver(report: dict[str, object], notify: Callable[..., bool] | None) -> bool:
    title, body = notification_for(report)
    sender = notify or desktop_notification
    try:
        if notify is None:
            return desktop_notification(title=title, body=body)
        return bool(sender(title=title, body=body))
    except (OSError, subprocess.TimeoutExpired):
        return False


def _base_report(*, publish: bool, status: str, error: str | None = None) -> dict[str, object]:
    return {
        "status": status,
        "stage": status,
        "success": status in {"completed", "diagnostic"},
        "publish": publish,
        "finished_at": _now_iso(),
        "error": error,
        "noon_timer_changed": False,
        "title": None,
        "instagram_permalink": None,
    }


def _read_report(directory: Path | None) -> dict[str, object]:
    path = (directory or report_directory()) / REPORT_NAME
    if not path.is_file():
        return {"status": "failed", "success": False, "publish": False, "error": "Report missing."}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "failed", "success": False, "publish": False, "error": "Report unreadable."}
    return loaded if isinstance(loaded, dict) else {"status": "failed", "success": False, "publish": False}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_argv(command: list[str]) -> str:
    """Format argv for logs. Env assignments never include token file contents."""
    return " ".join(command)


def _worker_failure_detail(
    completed: subprocess.CompletedProcess[str],
    payload: dict[str, object],
) -> str:
    existing = str(payload.get("error") or "").strip()
    if existing:
        return existing
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    # Prefer stderr; never invent credentials. Cap length for the report.
    blob = stderr or stdout
    if blob:
        return f"Worker exited {completed.returncode}: {blob[:800]}"
    return (
        f"Worker exited {completed.returncode} without updating the rehearsal report. "
        "Inhibit must wrap runuser; check the service journal for the worker argv."
    )


def _run_worker(command: list[str]) -> subprocess.CompletedProcess[str]:
    """
    Run the inhibited paint process.

    Stdio is inherited so journald captures Ollama/sd-cli progress. Output is
    not captured here — a previous capture_output=True path hid the early exit.
    """
    return subprocess.run(
        command,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=False,
        text=True,
        timeout=WORK_TIMEOUT_SECONDS,
        check=False,
    )


def _subprocess(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _system_state() -> str:
    try:
        completed = _subprocess(["systemctl", "is-system-running"])
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or "").strip()


def _idle() -> bool | None:
    from anskar.daily.power import session_idle_hint

    # Prefer the artist account when the service runs as root.
    user = os.environ.get("ANSKAR_TEST_USER") or os.environ.get("USER") or ""
    if user and user != "root":
        previous = os.environ.get("USER")
        os.environ["USER"] = user
        try:
            return session_idle_hint()
        finally:
            if previous is None:
                os.environ.pop("USER", None)
            else:
                os.environ["USER"] = previous
    return session_idle_hint()


def _foreign() -> list[str] | None:
    if shutil.which("systemd-inhibit") is None:
        return None
    try:
        completed = _subprocess(["systemd-inhibit", "--list"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return parse_foreign_inhibitors(completed.stdout or "")


def _suspend_job_queued() -> bool | None:
    """True when systemd lists a suspend/sleep job after the request."""
    try:
        completed = _subprocess(["systemctl", "list-jobs", "--no-legend"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    text = (completed.stdout or "").lower()
    if "suspend.target" in text or "sleep.target" in text or "suspend" in text:
        return True
    return False
