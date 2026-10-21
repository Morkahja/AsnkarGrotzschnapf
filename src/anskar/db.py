"""SQLite connection and schema for the poetry archive."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS poems (
    id INTEGER PRIMARY KEY,
    collection_id TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    author_death_year INTEGER,
    author_death_year_source TEXT,
    author_death_year_verified INTEGER NOT NULL CHECK (author_death_year_verified IN (0, 1)),
    origin TEXT,
    origin_source TEXT,
    original_language TEXT NOT NULL,
    text_language TEXT NOT NULL,
    translator TEXT,
    translator_death_year INTEGER,
    translator_death_year_source TEXT,
    translator_death_year_verified INTEGER NOT NULL CHECK (translator_death_year_verified IN (0, 1)),
    body TEXT NOT NULL,
    source_url TEXT NOT NULL,
    license_name TEXT NOT NULL,
    license_info TEXT NOT NULL,
    rights_verification_status TEXT NOT NULL CHECK (
        rights_verification_status IN ('unverified', 'documented', 'verified')
    ),
    commercial_use_sweden TEXT NOT NULL CHECK (
        commercial_use_sweden IN ('yes', 'no', 'unknown')
    ),
    imported_at TEXT NOT NULL,
    UNIQUE (source_url, title),
    CHECK (
        commercial_use_sweden != 'yes' OR (
            author_death_year IS NOT NULL
            AND author_death_year_verified = 1
            AND author_death_year_source IS NOT NULL
            AND length(trim(author_death_year_source)) > 0
            AND rights_verification_status = 'documented'
            AND length(trim(body)) > 0
            AND length(trim(source_url)) > 0
            AND length(trim(license_info)) > 0
            AND (
                translator IS NULL OR (
                    translator_death_year IS NOT NULL
                    AND translator_death_year_verified = 1
                    AND translator_death_year_source IS NOT NULL
                )
            )
            AND (
                translator IS NOT NULL OR original_language = text_language
            )
        )
    )
);

CREATE TABLE IF NOT EXISTS selection_history (
    id INTEGER PRIMARY KEY,
    poem_id INTEGER NOT NULL REFERENCES poems(id),
    selected_at TEXT NOT NULL,
    confirmed_at TEXT,
    skipped_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'skipped')),
    CHECK (
        (status = 'pending' AND confirmed_at IS NULL AND skipped_at IS NULL)
        OR (status = 'confirmed' AND confirmed_at IS NOT NULL AND skipped_at IS NULL)
        OR (status = 'skipped' AND skipped_at IS NOT NULL AND confirmed_at IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_selection
    ON selection_history (status)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS artworks (
    id INTEGER PRIMARY KEY,
    poem_id INTEGER NOT NULL REFERENCES poems(id),
    selection_id INTEGER NOT NULL REFERENCES selection_history(id),
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL CHECK (length(prompt_sha256) = 64),
    model TEXT NOT NULL,
    seed INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    duration_seconds REAL,
    settings_json TEXT NOT NULL,
    output_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_succeeded_artwork_per_poem
    ON artworks (poem_id)
    WHERE status = 'succeeded';

CREATE UNIQUE INDEX IF NOT EXISTS one_running_artwork_per_poem
    ON artworks (poem_id)
    WHERE status = 'running';
"""


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        initialize(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
