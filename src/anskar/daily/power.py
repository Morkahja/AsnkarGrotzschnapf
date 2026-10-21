"""Sleep inhibit and safe after-run decisions. Never force-suspend."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


SWEDISH_TZ = "Europe/Stockholm"

# What systemd-inhibit blocks while Anskar is painting/publishing.
INHIBIT_WHAT = "idle:sleep:handle-lid-switch"
INHIBIT_WHO = "AnskarGrotzschnapf"
INHIBIT_WHY = "Daily artwork production"

WAKEALARM = Path("/sys/class/rtc/rtc0/wakealarm")


@dataclass(frozen=True)
class AfterRunDecision:
    """What to do when a scheduled daily-run finishes."""

    action: str  # stay_awake | leave_to_idle_manager
    reason: str
    force_suspend: bool = False  # always False — kept explicit for tests


def inhibit_argv(argv: list[str]) -> list[str]:
    """Wrap any argv in systemd-inhibit (block idle/sleep for the child lifetime)."""
    if not argv:
        raise ValueError("inhibit_argv requires a non-empty command")
    return [
        "/usr/bin/systemd-inhibit",
        f"--what={INHIBIT_WHAT}",
        f"--who={INHIBIT_WHO}",
        f"--why={INHIBIT_WHY}",
        "--mode=block",
        *argv,
    ]


def inhibit_command(python: str, *args: str) -> list[str]:
    """Build the ExecStart argv that keeps the machine awake for one job."""
    return inhibit_argv([python, *args])


def session_idle_hint(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool | None:
    """
    Return True if loginctl reports the session idle, False if active, None if unknown.

    Unknown means: leave the computer awake and let KDE handle sleep.
    """
    if not shutil.which("loginctl"):
        return None
    run = runner or subprocess.run
    try:
        user = os.environ.get("USER") or ""
        if not user:
            return None
        session = run(
            ["loginctl", "show-user", user, "-p", "Sessions", "--value"],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if session.returncode != 0:
        return None
    session_ids = [part for part in session.stdout.strip().split() if part]
    if not session_ids:
        return None
    idle_votes: list[bool] = []
    for session_id in session_ids:
        try:
            hint = run(
                ["loginctl", "show-session", session_id, "-p", "IdleHint", "--value"],
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if hint.returncode != 0:
            return None
        value = hint.stdout.strip().lower()
        if value == "yes":
            idle_votes.append(True)
        elif value == "no":
            idle_votes.append(False)
        else:
            return None
    if not idle_votes:
        return None
    # Any active graphical session means the user is present.
    if any(not idle for idle in idle_votes):
        return False
    return True


def decide_after_run(
    *,
    scheduled: bool | None = None,
    idle: bool | None = None,
) -> AfterRunDecision:
    """
    Decide whether automatic return-to-sleep is appropriate.

    Never returns force_suspend=True. When unsure, stay awake and rely on KDE.
    """
    is_scheduled = (
        bool(os.environ.get("ANSKAR_SCHEDULED")) if scheduled is None else scheduled
    )
    idle_state = session_idle_hint() if idle is None else idle

    if not is_scheduled:
        return AfterRunDecision(
            action="stay_awake",
            reason="This run was not started by the noon timer; leave power management alone.",
        )
    if idle_state is None:
        return AfterRunDecision(
            action="stay_awake",
            reason=(
                "Session idle status could not be determined. "
                "Leaving the computer awake for KDE inactivity-based suspend."
            ),
        )
    if idle_state is False:
        return AfterRunDecision(
            action="stay_awake",
            reason="The user session is active. Never force suspension.",
        )
    return AfterRunDecision(
        action="leave_to_idle_manager",
        reason=(
            "Scheduled run finished and the session looks idle. "
            "Not forcing suspend — KDE/systemd idle suspend may sleep when appropriate."
        ),
    )


def verify_rtc_wake(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    wakealarm: Path = WAKEALARM,
) -> dict[str, object]:
    """Read-only check that RTC wakealarm exists. Does not suspend or publish."""
    rtcwake = shutil.which("rtcwake")
    report: dict[str, object] = {
        "wakealarm_path": str(wakealarm),
        "wakealarm_exists": wakealarm.is_file(),
        "rtcwake": rtcwake,
        "suspended": False,
        "published": False,
    }
    if rtcwake:
        run = runner or subprocess.run
        try:
            shown = run(
                [rtcwake, "-m", "show"],
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            report["rtcwake_show"] = (shown.stdout or shown.stderr or "").strip()
            report["rtcwake_ok"] = shown.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            report["rtcwake_ok"] = False
            report["rtcwake_error"] = str(exc)
    return report
