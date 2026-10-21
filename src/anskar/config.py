"""Paths for the local archive."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def database_path() -> Path:
    override = os.environ.get("ANSKAR_DB")
    if override:
        return Path(override)
    return PROJECT_ROOT / "data" / "archive.sqlite"
