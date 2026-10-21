"""Publish one pack to Etsy as a digital download."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from anskar.config import PROJECT_ROOT
from anskar.etsy.client import EtsyClient, listing_url
from anskar.etsy.credentials import (
    EtsyCredentials,
    ensure_fresh_credentials,
    etsy_configured,
)

# Re-export for callers.
__all__ = ["etsy_configured", "publish_pack", "published_listing"]
from anskar.etsy.errors import EtsyError
from anskar.etsy.listing import assert_etsy_ready, digital_files, listing_fields, load_pack, preview_image
from anskar.printmaker import packs_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS etsy_listings (
    id INTEGER PRIMARY KEY,
    pack_id TEXT NOT NULL,
    artwork_id TEXT NOT NULL,
    listing_id TEXT,
    listing_url TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'prepared', 'draft_created', 'assets_uploaded', 'published', 'failed', 'dry_run'
    )),
    error_kind TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_published_etsy_artwork
    ON etsy_listings (artwork_id)
    WHERE state = 'published';
"""


def etsy_database_path() -> Path:
    override = os.environ.get("ANSKAR_ETSY_DB")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / "data" / "etsy.sqlite"


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def publish_pack(
    pack_id: str,
    *,
    dry_run: bool = False,
    packs: Path | None = None,
    database: Path | None = None,
    credentials: EtsyCredentials | None = None,
    client: EtsyClient | None = None,
) -> dict[str, object]:
    root = packs or packs_dir()
    folder, manifest = load_pack(pack_id, packs=root)
    assert_etsy_ready(manifest, folder)
    artwork_id = str(manifest.get("artwork_id") or pack_id)
    db = database or etsy_database_path()
    with connect(db) as connection:
        existing = connection.execute(
            "SELECT * FROM etsy_listings WHERE artwork_id = ? AND state = 'published' ORDER BY id DESC LIMIT 1",
            (artwork_id,),
        ).fetchone()
        if existing is not None:
            raise EtsyError("This artwork already has a published Etsy listing.", kind="duplicate")
    fields = listing_fields(manifest)
    if dry_run:
        digital_files(folder)
        preview_image(folder)
        with connect(db) as connection:
            _insert(
                connection,
                pack_id=pack_id,
                artwork_id=artwork_id,
                state="dry_run",
            )
        return {
            "dry_run": True,
            "published": False,
            "pack_id": pack_id,
            "title": fields["title"],
            "tags": fields["tags"],
            "digital_files": [path.name for path in digital_files(folder)],
        }
    stored = credentials or ensure_fresh_credentials()
    graph = client or EtsyClient(stored)
    with connect(db) as connection:
        attempt_id = _insert(
            connection,
            pack_id=pack_id,
            artwork_id=artwork_id,
            state="prepared",
        )
    try:
        draft = graph.create_draft_listing(fields)
        listing_id = str(draft.get("listing_id") or draft.get("listing_id") or draft.get("id") or "")
        if not listing_id:
            raise EtsyError("Etsy did not return a listing id.", kind="api")
        with connect(db) as connection:
            _update(connection, attempt_id, state="draft_created", listing_id=listing_id)
        graph.upload_listing_image(listing_id, preview_image(folder))
        for path in digital_files(folder):
            graph.upload_listing_file(listing_id, path)
        with connect(db) as connection:
            _update(connection, attempt_id, state="assets_uploaded")
        published = graph.publish_listing(listing_id)
        state = str(published.get("state") or "active")
        url = listing_url(listing_id)
        with connect(db) as connection:
            _update(
                connection,
                attempt_id,
                state="published",
                listing_id=listing_id,
                listing_url=url,
            )
        _mark_pack_etsy(folder, listing_id=listing_id, listing_url=url)
        return {
            "dry_run": False,
            "published": True,
            "pack_id": pack_id,
            "listing_id": listing_id,
            "listing_url": url,
            "state": state,
            "title": fields["title"],
        }
    except EtsyError as exc:
        with connect(db) as connection:
            _update(
                connection,
                attempt_id,
                state="failed",
                error_kind=exc.kind,
                error_message=str(exc),
            )
        raise


def published_listing(artwork_id: str, *, database: Path | None = None) -> dict[str, object] | None:
    with connect(database or etsy_database_path()) as connection:
        row = connection.execute(
            "SELECT * FROM etsy_listings WHERE artwork_id = ? AND state = 'published' ORDER BY id DESC LIMIT 1",
            (artwork_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "pack_id": row["pack_id"],
        "artwork_id": row["artwork_id"],
        "listing_id": row["listing_id"],
        "listing_url": row["listing_url"],
        "state": row["state"],
    }


def _insert(
    connection: sqlite3.Connection,
    *,
    pack_id: str,
    artwork_id: str,
    state: str,
) -> int:
    now = _now()
    cursor = connection.execute(
        """
        INSERT INTO etsy_listings (pack_id, artwork_id, state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (pack_id, artwork_id, state, now, now),
    )
    return int(cursor.lastrowid)


def _update(connection: sqlite3.Connection, attempt_id: int, **fields: object) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    connection.execute(
        f"UPDATE etsy_listings SET {assignments}, updated_at = ? WHERE id = ?",
        (*fields.values(), _now(), attempt_id),
    )


def _mark_pack_etsy(folder: Path, *, listing_id: str, listing_url: str) -> None:
    manifest_path = folder / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    payload["etsy_listing_id"] = listing_id
    payload["etsy_listing_url"] = listing_url
    payload["etsy_published"] = True
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
