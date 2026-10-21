"""Desktop notifications for daily production stages (Plasma / notify-send)."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]

APP_NAME = "Anskar Grotzschnapf"


def notifications_enabled() -> bool:
    """Tests set ANSKAR_NOTIFY=0 so notify-send is not spammed."""
    value = os.environ.get("ANSKAR_NOTIFY", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def desktop_notification(
    *,
    title: str,
    body: str,
    runner: Runner | None = None,
) -> bool:
    """Send a desktop notification. Failure never stops the pipeline."""
    if not notifications_enabled():
        return False
    command = ["notify-send", f"--app-name={APP_NAME}", "--expire-time=12000", title, body]
    if runner is None and shutil.which("notify-send") is None:
        return False
    run = runner or _subprocess
    try:
        completed = run(command)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def notify_stage(stage: str, detail: str = "") -> bool:
    """Notify for a named daily-run stage. `detail` is optional subtitle text."""
    titles = {
        "start": "Daily run started",
        "generate": "Generating artwork",
        "instagram": "Publishing to Instagram",
        "etsy": "Publishing to Etsy",
        "etsy_queued": "Etsy listing queued",
        "complete": "Daily run finished",
        "failed": "Daily run failed",
    }
    title = titles.get(stage, f"Anskar: {stage}")
    body = detail.strip() or {
        "start": "Selecting a poem and preparing today's artwork.",
        "generate": "Local image generation is starting now.",
        "instagram": "Uploading today's artwork to Instagram.",
        "etsy": "Creating today's Etsy listing.",
        "etsy_queued": "Etsy is not ready to publish; the pack stays queued.",
        "complete": "Today's artwork is done.",
        "failed": "Something went wrong. Check the daily run status.",
    }.get(stage, "")
    return desktop_notification(title=title, body=body)


def _subprocess(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
