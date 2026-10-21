"""Prevent overlapping daily runs."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from anskar.config import PROJECT_ROOT
from anskar.daily.errors import DailyError


def lock_path() -> Path:
    override = os.environ.get("ANSKAR_DAILY_LOCK")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / "data" / "daily.lock"


@contextmanager
def exclusive_lock(path: Path | None = None) -> Iterator[None]:
    file = path or lock_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(file, "a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DailyError(
                "A daily run is already active. Wait for it to finish or inspect daily-status.",
                kind="busy",
            ) from exc
        except ImportError:
            if file.exists() and file.read_text(encoding="utf-8").strip().startswith("pid="):
                raise DailyError(
                    "A daily run is already active. Wait for it to finish or inspect daily-status.",
                    kind="busy",
                )
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        try:
            handle.seek(0)
            handle.truncate()
        except OSError:
            pass
        handle.close()
