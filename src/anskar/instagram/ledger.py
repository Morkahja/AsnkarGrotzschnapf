"""Publication attempts. A published artwork is not sent again."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from anskar.instagram.errors import InstagramError

STATES = (
    "prepared",
    "dry_run",
    "container_created",
    "publishing",
    "published",
    "failed",
    "unknown",
)
BLOCKING = ("container_created", "publishing", "unknown")

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    pack_id TEXT NOT NULL,
    artwork_id TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    caption_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'prepared', 'dry_run', 'container_created', 'publishing',
        'published', 'failed', 'unknown'
    )),
    container_id TEXT,
    media_id TEXT,
    permalink TEXT,
    image_url TEXT,
    staging_key TEXT,
    error_kind TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_published_artwork
    ON attempts (artwork_id)
    WHERE state = 'published';
"""


def ledger_path(root: Path | None = None) -> Path:
    if root is not None:
        return root
    from anskar.config import PROJECT_ROOT

    return PROJECT_ROOT / "data" / "instagram.sqlite"


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(attempts)").fetchall()
    }
    if "staging_key" not in columns:
        connection.execute("ALTER TABLE attempts ADD COLUMN staging_key TEXT")


def published_attempt(connection: sqlite3.Connection, artwork_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM attempts WHERE artwork_id = ? AND state = 'published' ORDER BY id DESC LIMIT 1",
        (artwork_id,),
    ).fetchone()


def blocking_attempt(connection: sqlite3.Connection, artwork_id: str) -> sqlite3.Row | None:
    placeholders = ",".join("?" for _ in BLOCKING)
    return connection.execute(
        f"""
        SELECT * FROM attempts
        WHERE artwork_id = ? AND state IN ({placeholders})
        ORDER BY id DESC LIMIT 1
        """,
        (artwork_id, *BLOCKING),
    ).fetchone()


def recoverable_container_attempt(
    connection: sqlite3.Connection, artwork_id: str
) -> sqlite3.Row | None:
    """Latest attempt that already has a container and may still be publishable."""
    return connection.execute(
        """
        SELECT * FROM attempts
        WHERE artwork_id = ?
          AND container_id IS NOT NULL
          AND state IN ('container_created', 'publishing', 'unknown', 'failed')
        ORDER BY id DESC LIMIT 1
        """,
        (artwork_id,),
    ).fetchone()


def begin_attempt(
    connection: sqlite3.Connection,
    *,
    pack_id: str,
    artwork_id: str,
    image_sha256: str,
    caption_sha256: str,
    state: str,
    image_url: str | None = None,
    staging_key: str | None = None,
) -> int:
    if state not in STATES:
        raise InstagramError("The publication state is not valid.", kind="error")
    if state == "published" and published_attempt(connection, artwork_id) is not None:
        raise InstagramError("This artwork is already published on Instagram.", kind="duplicate")
    now = _now()
    cursor = connection.execute(
        """
        INSERT INTO attempts (
            pack_id, artwork_id, image_sha256, caption_sha256, state,
            image_url, staging_key, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pack_id,
            artwork_id,
            image_sha256,
            caption_sha256,
            state,
            image_url,
            staging_key,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def update_attempt(connection: sqlite3.Connection, attempt_id: int, **fields: object) -> None:
    allowed = {
        "state",
        "container_id",
        "media_id",
        "permalink",
        "image_url",
        "staging_key",
        "error_kind",
        "error_message",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise InstagramError("The publication record has an unknown field.", kind="error")
    if "state" in fields and fields["state"] not in STATES:
        raise InstagramError("The publication state is not valid.", kind="error")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    connection.execute(
        f"UPDATE attempts SET {assignments}, updated_at = ? WHERE id = ?",
        (*fields.values(), _now(), attempt_id),
    )


def list_attempts(connection: sqlite3.Connection, pack_id: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM attempts WHERE pack_id = ? ORDER BY id",
            (pack_id,),
        )
    )


def public_attempt(row: sqlite3.Row) -> dict[str, object]:
    keys = row.keys()
    staging_key = row["staging_key"] if "staging_key" in keys else None
    return {
        "id": row["id"],
        "pack_id": row["pack_id"],
        "artwork_id": row["artwork_id"],
        "state": row["state"],
        "container_id": row["container_id"],
        "media_id": row["media_id"],
        "permalink": row["permalink"],
        "staging_key": staging_key,
        "staged": bool(staging_key),
        "error_kind": row["error_kind"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
