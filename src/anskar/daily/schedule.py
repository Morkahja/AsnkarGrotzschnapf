"""Local systemd schedule for the daily pipeline (user timer + optional system wake)."""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from anskar.config import PROJECT_ROOT
from anskar.daily.errors import DailyError
from anskar.daily.power import decide_after_run, inhibit_command, verify_rtc_wake

SERVICE_NAME = "anskar-daily.service"
TIMER_NAME = "anskar-daily.timer"
ETSY_MAINTAIN_SERVICE = "anskar-etsy-maintain.service"
ETSY_MAINTAIN_TIMER = "anskar-etsy-maintain.timer"
NOON_CALENDAR = "*-*-* 12:00:00"
ETSY_MAINTAIN_CALENDAR = "*-*-* 0/6:15:00"


def unit_source_dir() -> Path:
    return PROJECT_ROOT / "systemd"


def system_unit_source_dir() -> Path:
    return PROJECT_ROOT / "systemd" / "system"


def user_unit_dir() -> Path:
    override = os.environ.get("ANSKAR_SYSTEMD_USER_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "systemd" / "user"


def system_staging_dir() -> Path:
    override = os.environ.get("ANSKAR_SYSTEMD_SYSTEM_STAGING")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / "data" / "systemd-system"


def default_python() -> str:
    override = os.environ.get("ANSKAR_PYTHON")
    if override:
        return override
    venv = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv.is_file():
        return str(venv)
    return shutil.which("python3") or "/usr/bin/python3"


def schedule_status() -> dict[str, object]:
    service = user_unit_dir() / SERVICE_NAME
    timer = user_unit_dir() / TIMER_NAME
    enabled = False
    active = False
    next_elapse = None
    if shutil.which("systemctl"):
        enabled = _systemctl(["--user", "is-enabled", TIMER_NAME]) == 0
        active = _systemctl(["--user", "is-active", TIMER_NAME]) == 0
        next_elapse = _systemctl_show(["--user", "show", TIMER_NAME, "-p", "NextElapseUSecRealtime", "--value"])
    system_timer = Path("/etc/systemd/system") / TIMER_NAME
    system_enabled = False
    system_active = False
    if shutil.which("systemctl") and system_timer.is_file():
        system_enabled = _systemctl(["is-enabled", TIMER_NAME]) == 0
        system_active = _systemctl(["is-active", TIMER_NAME]) == 0
    rtc = verify_rtc_wake()
    after = decide_after_run(scheduled=True, idle=None)
    etsy_service = user_unit_dir() / ETSY_MAINTAIN_SERVICE
    etsy_timer = user_unit_dir() / ETSY_MAINTAIN_TIMER
    etsy_enabled = False
    if shutil.which("systemctl") and etsy_timer.is_file():
        etsy_enabled = _systemctl(["--user", "is-enabled", ETSY_MAINTAIN_TIMER]) == 0
    return {
        "service_installed": service.is_file(),
        "timer_installed": timer.is_file(),
        "service_path": str(service),
        "timer_path": str(timer),
        "enabled": enabled,
        "active": active,
        "next_elapse": next_elapse,
        "on_calendar": NOON_CALENDAR,
        "persistent": False,
        "source_dir": str(unit_source_dir()),
        "system_timer_installed": system_timer.is_file(),
        "system_timer_path": str(system_timer),
        "system_enabled": system_enabled,
        "system_active": system_active,
        "system_staging_dir": str(system_staging_dir()),
        "wake_system": True,  # system timer capability; user timer does not wake from deep sleep
        "rtc": rtc,
        "return_to_sleep": (
            "The noon run never suspends the computer. "
            "Only a confirmed test-cycle may suspend, and only while the session stays idle."
        ),
        "after_run_policy": after.reason,
        "python": default_python(),
        "etsy_maintain_service_installed": etsy_service.is_file(),
        "etsy_maintain_timer_installed": etsy_timer.is_file(),
        "etsy_maintain_enabled": etsy_enabled,
        "etsy_maintain_on_calendar": ETSY_MAINTAIN_CALENDAR,
    }


def install_units(*, enable: bool = False) -> dict[str, object]:
    """Install or refresh the user timer/service (noon, Persistent=false, inhibit)."""
    source = unit_source_dir()
    service_src = source / SERVICE_NAME
    timer_src = source / TIMER_NAME
    if not service_src.is_file() or not timer_src.is_file():
        raise DailyError("The systemd unit templates are missing from the repository.", kind="error")
    _assert_timer_policy(timer_src.read_text(encoding="utf-8"))
    destination = user_unit_dir()
    destination.mkdir(parents=True, exist_ok=True)
    service_dst = destination / SERVICE_NAME
    timer_dst = destination / TIMER_NAME
    service_text = _render(service_src.read_text(encoding="utf-8"))
    timer_text = timer_src.read_text(encoding="utf-8")
    service_dst.write_text(service_text, encoding="utf-8")
    timer_dst.write_text(timer_text, encoding="utf-8")
    etsy_service_src = source / ETSY_MAINTAIN_SERVICE
    etsy_timer_src = source / ETSY_MAINTAIN_TIMER
    if etsy_service_src.is_file() and etsy_timer_src.is_file():
        (destination / ETSY_MAINTAIN_SERVICE).write_text(
            _render(etsy_service_src.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        (destination / ETSY_MAINTAIN_TIMER).write_text(
            etsy_timer_src.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    reloaded = False
    if shutil.which("systemctl"):
        reloaded = _systemctl(["--user", "daemon-reload"]) == 0
        if enable:
            if _systemctl(["--user", "enable", "--now", TIMER_NAME]) != 0:
                raise DailyError(
                    "Units were installed but enabling the timer failed. "
                    "Run `systemctl --user enable --now anskar-daily.timer` after fixing systemd.",
                    kind="error",
                )
            if (destination / ETSY_MAINTAIN_TIMER).is_file():
                _systemctl(["--user", "enable", "--now", ETSY_MAINTAIN_TIMER])
    elif enable:
        raise DailyError(
            "Units were written, but systemctl is not available so the timer was not enabled.",
            kind="error",
        )
    report = schedule_status()
    report["installed"] = True
    report["enabled_now"] = enable
    report["daemon_reloaded"] = reloaded
    report["inhibit_exec"] = " ".join(inhibit_command(default_python(), "-m", "anskar", "daily-run"))
    return report


def prepare_system_units() -> dict[str, object]:
    """
    Render system units into data/systemd-system/ for a later sudo install.

    Does not write to /etc, does not enable, does not disable the user timer.
    """
    source = system_unit_source_dir()
    service_src = source / SERVICE_NAME
    timer_src = source / TIMER_NAME
    if not service_src.is_file() or not timer_src.is_file():
        raise DailyError("The system unit templates are missing from systemd/system/.", kind="error")
    timer_text = timer_src.read_text(encoding="utf-8")
    _assert_timer_policy(timer_text)
    if "WakeSystem=true" not in timer_text:
        raise DailyError("The system timer template must set WakeSystem=true.", kind="error")
    staging = system_staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    service_dst = staging / SERVICE_NAME
    timer_dst = staging / TIMER_NAME
    service_dst.write_text(_render_system(service_src.read_text(encoding="utf-8")), encoding="utf-8")
    timer_dst.write_text(timer_text, encoding="utf-8")
    activation = _activation_commands(staging)
    return {
        "prepared": True,
        "staging_dir": str(staging),
        "service_path": str(service_dst),
        "timer_path": str(timer_dst),
        "enabled": False,
        "wake_system": True,
        "on_calendar": NOON_CALENDAR,
        "persistent": False,
        "activation_commands": activation,
        "rollback_commands": _rollback_commands(),
        "note": (
            "System units are staged only. Do not enable them without disabling the user "
            "timer first — two timers must not both publish."
        ),
        "return_to_sleep": (
            "After the oneshot finishes, Anskar does not call systemctl suspend. "
            "Return to sleep depends on KDE idle-suspend settings."
        ),
        "rtc": verify_rtc_wake(),
    }


def parse_next_elapse(value: object) -> datetime | None:
    """Parse systemd NextElapseUSecRealtime (µs epoch, or empty/n/a)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "0", "infinity"}:
        return None
    try:
        usec = int(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    if usec <= 0:
        return None
    return datetime.fromtimestamp(usec / 1_000_000, tz=timezone.utc).astimezone()


def production_next_elapse() -> datetime | None:
    """Next noon elapse from the active daily timer (system preferred when installed)."""
    status = schedule_status()
    user_next = parse_next_elapse(status.get("next_elapse"))
    if status.get("system_timer_installed"):
        system_raw = _systemctl_show(
            ["show", TIMER_NAME, "-p", "NextElapseUSecRealtime", "--value"]
        )
        system_next = parse_next_elapse(system_raw)
        if system_next is not None:
            return system_next
    return user_next


def _assert_timer_policy(timer_text: str) -> None:
    if "12:00:00" not in timer_text:
        raise DailyError("The timer template must schedule 12:00:00 noon.", kind="error")
    if "Persistent=true" in timer_text:
        raise DailyError(
            "The timer template must use Persistent=false to avoid catch-up backlog posts.",
            kind="error",
        )
    if "06:30" in timer_text:
        raise DailyError("The timer template still contains the old 06:30 schedule.", kind="error")
    if "RandomizedDelaySec=0" not in timer_text and "RandomizedDelaySec=0s" not in timer_text:
        raise DailyError(
            "The timer template must use RandomizedDelaySec=0 so wake starts promptly.",
            kind="error",
        )


def _render(template: str) -> str:
    return (
        template.replace("@PROJECT_ROOT@", str(PROJECT_ROOT))
        .replace("@PYTHON@", default_python())
        .replace("@HOME@", str(Path.home()))
        .replace("@USER@", pwd.getpwuid(os.getuid()).pw_name)
        .replace("@UID@", str(os.getuid()))
    )


def _render_system(template: str) -> str:
    user = pwd.getpwuid(os.getuid())
    return (
        template.replace("@PROJECT_ROOT@", str(PROJECT_ROOT))
        .replace("@PYTHON@", default_python())
        .replace("@HOME@", user.pw_dir)
        .replace("@USER@", user.pw_name)
        .replace("@UID@", str(user.pw_uid))
    )


def _activation_commands(staging: Path) -> list[str]:
    return [
        "systemctl --user disable --now anskar-daily.timer",
        f"sudo cp {staging / SERVICE_NAME} /etc/systemd/system/{SERVICE_NAME}",
        f"sudo cp {staging / TIMER_NAME} /etc/systemd/system/{TIMER_NAME}",
        "sudo systemctl daemon-reload",
        "sudo systemctl enable --now anskar-daily.timer",
        "systemctl list-timers anskar-daily.timer",
        "cat /sys/class/rtc/rtc0/wakealarm || true",
    ]


def _rollback_commands() -> list[str]:
    return [
        "sudo systemctl disable --now anskar-daily.timer",
        "sudo rm -f /etc/systemd/system/anskar-daily.service /etc/systemd/system/anskar-daily.timer",
        "sudo systemctl daemon-reload",
        "anskar schedule install --enable",
    ]


def _systemctl(args: list[str]) -> int:
    try:
        completed = subprocess.run(
            ["systemctl", *args],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return 1
    return completed.returncode


def _systemctl_show(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["systemctl", *args],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    value = (completed.stdout or "").strip()
    return value or None
