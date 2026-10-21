"""Persistent daily-run records with independent Instagram and Etsy states."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from anskar.config import PROJECT_ROOT
from anskar.daily.errors import DailyError
from anskar.daily.power import SWEDISH_TZ
from anskar.etsy.state import ETSY_LISTING_PENDING

STATES = (
    "selected",
    "generated",
    "pack_prepared",
    "instagram_published",
    "completed",
    "failed",
)

# Incomplete runs that block a new daily-run. Completed Instagram days are not active.
ACTIVE = (
    "selected",
    "generated",
    "pack_prepared",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN (
        'selected', 'generated', 'pack_prepared',
        'instagram_published', 'completed', 'failed'
    )),
    dry_run INTEGER NOT NULL DEFAULT 0,
    poem_id INTEGER,
    selection_id INTEGER,
    artwork_db_id INTEGER,
    pack_id TEXT,
    title TEXT,
    author TEXT,
    source_sha256 TEXT,
    visual_prompt TEXT,
    prompt_sha256 TEXT,
    image_path TEXT,
    image_sha256 TEXT,
    seed INTEGER,
    model_id TEXT,
    settings_json TEXT,
    duration_seconds REAL,
    artwork_status TEXT,
    instagram_status TEXT,
    etsy_status TEXT,
    instagram_media_id TEXT,
    instagram_permalink TEXT,
    etsy_listing_id TEXT,
    etsy_listing_url TEXT,
    selection_log TEXT,
    error_kind TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
"""


def database_path() -> Path:
    override = os.environ.get("ANSKAR_DAILY_DB")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / "data" / "daily.sqlite"


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    file = path or database_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(file)
    try:
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        _ensure_columns(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_columns(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(daily_runs)").fetchall()}
    additions = {
        "artwork_status": "TEXT",
        "instagram_status": "TEXT",
        "etsy_status": "TEXT",
        "selection_log": "TEXT",
    }
    for name, decl in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE daily_runs ADD COLUMN {name} {decl}")


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"daily-{stamp}-{uuid.uuid4().hex[:8]}"


def active_run(connection: sqlite3.Connection) -> sqlite3.Row | None:
    placeholders = ",".join("?" for _ in ACTIVE)
    return connection.execute(
        f"""
        SELECT * FROM daily_runs
        WHERE status IN ({placeholders})
        ORDER BY created_at DESC LIMIT 1
        """,
        ACTIVE,
    ).fetchone()


def swedish_calendar_day(moment: datetime | None = None) -> str:
    """YYYY-MM-DD in Europe/Stockholm (handles daylight-saving)."""
    clock = moment or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(ZoneInfo(SWEDISH_TZ)).strftime("%Y-%m-%d")


def completed_live_run_for_utc_day(connection: sqlite3.Connection, day: str) -> sqlite3.Row | None:
    """Legacy helper: day is YYYY-MM-DD compared to the UTC date prefix of created_at."""
    return connection.execute(
        """
        SELECT * FROM daily_runs
        WHERE dry_run = 0
          AND status = 'completed'
          AND substr(created_at, 1, 10) = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (day,),
    ).fetchone()


def completed_live_run_for_swedish_day(
    connection: sqlite3.Connection, day: str
) -> sqlite3.Row | None:
    """day is YYYY-MM-DD in Europe/Stockholm. Dry-runs are ignored."""
    rows = connection.execute(
        """
        SELECT * FROM daily_runs
        WHERE dry_run = 0 AND status = 'completed'
        ORDER BY created_at DESC LIMIT 64
        """
    ).fetchall()
    for row in rows:
        created = datetime.fromisoformat(str(row["created_at"]))
        if swedish_calendar_day(created) == day:
            return row
    return None


def get_run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM daily_runs WHERE id = ?", (run_id,)).fetchone()


def begin_run(connection: sqlite3.Connection, *, dry_run: bool) -> str:
    existing = active_run(connection)
    if existing is not None:
        raise DailyError(
            f"An incomplete daily run already exists: {existing['id']} ({existing['status']}). "
            f"Resume it with `anskar daily-resume {existing['id']}`.",
            kind="busy",
        )
    if not dry_run:
        today = swedish_calendar_day()
        prior = completed_live_run_for_swedish_day(connection, today)
        if prior is not None:
            raise DailyError(
                f"A live daily artwork was already completed today ({prior['id']}). "
                "At most one new production artwork is created per Swedish local day.",
                kind="busy",
            )
    run_id = new_run_id()
    now = _now()
    connection.execute(
        """
        INSERT INTO daily_runs (
            id, status, dry_run, artwork_status, instagram_status, etsy_status,
            created_at, updated_at
        ) VALUES (?, 'selected', ?, 'pending', 'pending', ?, ?, ?)
        """,
        (run_id, 1 if dry_run else 0, ETSY_LISTING_PENDING, now, now),
    )
    return run_id


def update_run(connection: sqlite3.Connection, run_id: str, **fields: object) -> None:
    allowed = {
        "status",
        "poem_id",
        "selection_id",
        "artwork_db_id",
        "pack_id",
        "title",
        "author",
        "source_sha256",
        "visual_prompt",
        "prompt_sha256",
        "image_path",
        "image_sha256",
        "seed",
        "model_id",
        "settings_json",
        "duration_seconds",
        "artwork_status",
        "instagram_status",
        "etsy_status",
        "instagram_media_id",
        "instagram_permalink",
        "etsy_listing_id",
        "etsy_listing_url",
        "selection_log",
        "error_kind",
        "error_message",
        "completed_at",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise DailyError(f"Unknown daily-run field: {sorted(unknown)[0]}", kind="error")
    if "status" in fields and fields["status"] not in STATES:
        raise DailyError("The daily-run status is not valid.", kind="error")
    if not fields:
        return
    values: list[object] = []
    for name, value in fields.items():
        if name == "selection_log" and not isinstance(value, (str, type(None))):
            values.append(json.dumps(value, ensure_ascii=False))
        else:
            values.append(value)
    assignments = ", ".join(f"{name} = ?" for name in fields)
    connection.execute(
        f"UPDATE daily_runs SET {assignments}, updated_at = ? WHERE id = ?",
        (*values, _now(), run_id),
    )


def list_runs(connection: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM daily_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    )


def public_run(row: sqlite3.Row) -> dict[str, object]:
    keys = row.keys()
    artwork_status = row["artwork_status"] if "artwork_status" in keys else None
    instagram_status = row["instagram_status"] if "instagram_status" in keys else None
    etsy_status = row["etsy_status"] if "etsy_status" in keys else None
    if not artwork_status:
        if row["image_path"]:
            artwork_status = "generated"
        elif row["poem_id"]:
            artwork_status = "selected"
        else:
            artwork_status = "pending"
    if not instagram_status:
        instagram_status = "published" if row["instagram_media_id"] else "pending"
    if not etsy_status:
        if row["etsy_listing_id"]:
            etsy_status = "published"
        else:
            etsy_status = ETSY_LISTING_PENDING
    return {
        "id": row["id"],
        "status": row["status"],
        "daily_status": row["status"],
        "dry_run": bool(row["dry_run"]),
        "poem_id": row["poem_id"],
        "selection_id": row["selection_id"],
        "artwork_db_id": row["artwork_db_id"],
        "pack_id": row["pack_id"],
        "title": row["title"],
        "author": row["author"],
        "prompt_sha256": row["prompt_sha256"],
        "image_path": row["image_path"],
        "image_sha256": row["image_sha256"],
        "seed": row["seed"],
        "model_id": row["model_id"],
        "artwork_status": artwork_status,
        "instagram_status": instagram_status,
        "etsy_status": etsy_status,
        "instagram_media_id": row["instagram_media_id"],
        "instagram_permalink": row["instagram_permalink"],
        "etsy_listing_id": row["etsy_listing_id"],
        "etsy_listing_url": row["etsy_listing_url"],
        "selection_log": (
            json.loads(row["selection_log"])
            if "selection_log" in keys and row["selection_log"]
            else []
        ),
        "error_kind": row["error_kind"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "auto_approved": True,
    }


def settings_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
