"""SQLite store for Etsy listings, sales, and pricing history."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from anskar.config import PROJECT_ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS etsy_listings (
    id INTEGER PRIMARY KEY,
    pack_id TEXT NOT NULL,
    artwork_id TEXT NOT NULL,
    listing_id TEXT,
    listing_url TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'prepared', 'draft_created', 'assets_uploaded', 'active',
        'sold', 'expired', 'failed', 'dry_run'
    )),
    price_r INTEGER,
    price_s INTEGER,
    original_price_sek REAL,
    current_price_sek REAL,
    currency TEXT,
    discount_applied INTEGER NOT NULL DEFAULT 0,
    activated_at TEXT,
    discounted_at TEXT,
    sold_at TEXT,
    expired_at TEXT,
    etsy_transaction_id TEXT,
    quantity INTEGER NOT NULL DEFAULT 1,
    error_kind TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_live_etsy_artwork
    ON etsy_listings (artwork_id)
    WHERE state IN ('prepared', 'draft_created', 'assets_uploaded', 'active');

CREATE UNIQUE INDEX IF NOT EXISTS one_terminal_etsy_artwork
    ON etsy_listings (artwork_id)
    WHERE state IN ('sold', 'expired');

CREATE TABLE IF NOT EXISTS etsy_sales (
    id INTEGER PRIMARY KEY,
    transaction_id TEXT NOT NULL UNIQUE,
    receipt_id TEXT,
    listing_id TEXT NOT NULL,
    artwork_id TEXT,
    pack_id TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etsy_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

LEGACY_STATE_MAP = {
    "published": "active",
}


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
        _migrate(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def completed_sales_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM etsy_meta WHERE key = 'completed_sales_count'"
    ).fetchone()
    if row is not None:
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            pass
    counted = connection.execute("SELECT COUNT(*) AS n FROM etsy_sales").fetchone()
    return int(counted["n"] if counted else 0)


def set_completed_sales_count(connection: sqlite3.Connection, count: int) -> None:
    connection.execute(
        """
        INSERT INTO etsy_meta (key, value) VALUES ('completed_sales_count', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(int(count)),),
    )


def increment_completed_sales(connection: sqlite3.Connection) -> int:
    """Bump the counter. Call only after a new etsy_sales row is inserted."""
    row = connection.execute(
        "SELECT value FROM etsy_meta WHERE key = 'completed_sales_count'"
    ).fetchone()
    if row is None:
        # Meta missing: trust the sales table (includes the row just inserted).
        counted = connection.execute("SELECT COUNT(*) AS n FROM etsy_sales").fetchone()
        nxt = int(counted["n"] if counted else 0)
    else:
        try:
            nxt = int(row["value"]) + 1
        except (TypeError, ValueError):
            nxt = 1
    set_completed_sales_count(connection, nxt)
    return nxt


def find_by_artwork(
    connection: sqlite3.Connection,
    artwork_id: str,
    *,
    states: tuple[str, ...] | None = None,
) -> sqlite3.Row | None:
    if states:
        placeholders = ",".join("?" for _ in states)
        return connection.execute(
            f"""
            SELECT * FROM etsy_listings
            WHERE artwork_id = ? AND state IN ({placeholders})
            ORDER BY id DESC LIMIT 1
            """,
            (artwork_id, *states),
        ).fetchone()
    return connection.execute(
        "SELECT * FROM etsy_listings WHERE artwork_id = ? ORDER BY id DESC LIMIT 1",
        (artwork_id,),
    ).fetchone()


def find_by_listing_id(connection: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM etsy_listings WHERE listing_id = ? ORDER BY id DESC LIMIT 1",
        (listing_id,),
    ).fetchone()


def list_offerings(
    connection: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT * FROM etsy_listings
            WHERE state IN ('active', 'sold', 'expired', 'draft_created', 'assets_uploaded', 'failed')
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def active_listings(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM etsy_listings WHERE state = 'active' ORDER BY id ASC"
        ).fetchall()
    )


def transaction_seen(connection: sqlite3.Connection, transaction_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM etsy_sales WHERE transaction_id = ?",
        (transaction_id,),
    ).fetchone()
    return row is not None


def record_sale(
    connection: sqlite3.Connection,
    *,
    transaction_id: str,
    receipt_id: str | None,
    listing_id: str,
    artwork_id: str | None,
    pack_id: str | None,
) -> bool:
    """Insert a sale once. Returns True when newly recorded."""
    if transaction_seen(connection, transaction_id):
        return False
    connection.execute(
        """
        INSERT INTO etsy_sales (
            transaction_id, receipt_id, listing_id, artwork_id, pack_id, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            transaction_id,
            receipt_id,
            listing_id,
            artwork_id,
            pack_id,
            _now(),
        ),
    )
    increment_completed_sales(connection)
    return True


def insert_listing(
    connection: sqlite3.Connection,
    *,
    pack_id: str,
    artwork_id: str,
    state: str,
    price_r: int | None = None,
    price_s: int | None = None,
    original_price_sek: float | None = None,
    current_price_sek: float | None = None,
    currency: str | None = None,
    quantity: int = 1,
) -> int:
    now = _now()
    cursor = connection.execute(
        """
        INSERT INTO etsy_listings (
            pack_id, artwork_id, state,
            price_r, price_s, original_price_sek, current_price_sek, currency,
            quantity, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pack_id,
            artwork_id,
            state,
            price_r,
            price_s,
            original_price_sek,
            current_price_sek,
            currency,
            quantity,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def update_listing(connection: sqlite3.Connection, attempt_id: int, **fields: object) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    connection.execute(
        f"UPDATE etsy_listings SET {assignments}, updated_at = ? WHERE id = ?",
        (*fields.values(), _now(), attempt_id),
    )


def row_public(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def _migrate(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'etsy_listings'"
    ).fetchone()
    sql = (row["sql"] if row else "") or ""
    needs_rebuild = "'sold'" not in sql or "'active'" not in sql or "'expired'" not in sql
    if needs_rebuild and row is not None:
        _rebuild_listings_table(connection)
        return
    columns = {
        info["name"]
        for info in connection.execute("PRAGMA table_info(etsy_listings)").fetchall()
    }
    additions = {
        "price_r": "INTEGER",
        "price_s": "INTEGER",
        "original_price_sek": "REAL",
        "current_price_sek": "REAL",
        "currency": "TEXT",
        "discount_applied": "INTEGER NOT NULL DEFAULT 0",
        "activated_at": "TEXT",
        "discounted_at": "TEXT",
        "sold_at": "TEXT",
        "expired_at": "TEXT",
        "etsy_transaction_id": "TEXT",
        "quantity": "INTEGER NOT NULL DEFAULT 1",
    }
    for name, decl in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE etsy_listings ADD COLUMN {name} {decl}")


def _rebuild_listings_table(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE etsy_listings RENAME TO etsy_listings_legacy")
    connection.executescript(
        """
        CREATE TABLE etsy_listings (
            id INTEGER PRIMARY KEY,
            pack_id TEXT NOT NULL,
            artwork_id TEXT NOT NULL,
            listing_id TEXT,
            listing_url TEXT,
            state TEXT NOT NULL CHECK (state IN (
                'prepared', 'draft_created', 'assets_uploaded', 'active',
                'sold', 'expired', 'failed', 'dry_run'
            )),
            price_r INTEGER,
            price_s INTEGER,
            original_price_sek REAL,
            current_price_sek REAL,
            currency TEXT,
            discount_applied INTEGER NOT NULL DEFAULT 0,
            activated_at TEXT,
            discounted_at TEXT,
            sold_at TEXT,
            expired_at TEXT,
            etsy_transaction_id TEXT,
            quantity INTEGER NOT NULL DEFAULT 1,
            error_kind TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_live_etsy_artwork
            ON etsy_listings (artwork_id)
            WHERE state IN ('prepared', 'draft_created', 'assets_uploaded', 'active');
        CREATE UNIQUE INDEX IF NOT EXISTS one_terminal_etsy_artwork
            ON etsy_listings (artwork_id)
            WHERE state IN ('sold', 'expired');
        """
    )
    legacy_cols = {
        info["name"]
        for info in connection.execute("PRAGMA table_info(etsy_listings_legacy)").fetchall()
    }
    connection.execute(
        """
        INSERT INTO etsy_listings (
            id, pack_id, artwork_id, listing_id, listing_url, state,
            error_kind, error_message, created_at, updated_at, quantity
        )
        SELECT
            id, pack_id, artwork_id, listing_id, listing_url,
            CASE state WHEN 'published' THEN 'active' ELSE state END,
            error_kind, error_message, created_at, updated_at, 1
        FROM etsy_listings_legacy
        """
    )
    connection.execute("DROP TABLE etsy_listings_legacy")
    del legacy_cols  # kept for clarity that we inspected the old table


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
