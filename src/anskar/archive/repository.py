"""SQLite reads and writes for poems and selection history."""

import sqlite3
from datetime import datetime, timezone

from anskar.archive.models import Poem, PoemDraft, Selection

POEM_COLUMNS = """
    poems.id AS id,
    poems.collection_id AS collection_id,
    poems.title AS title,
    poems.author AS author,
    poems.author_death_year AS author_death_year,
    poems.author_death_year_source AS author_death_year_source,
    poems.author_death_year_verified AS author_death_year_verified,
    poems.origin AS origin,
    poems.origin_source AS origin_source,
    poems.original_language AS original_language,
    poems.text_language AS text_language,
    poems.translator AS translator,
    poems.translator_death_year AS translator_death_year,
    poems.translator_death_year_source AS translator_death_year_source,
    poems.translator_death_year_verified AS translator_death_year_verified,
    poems.body AS body,
    poems.source_url AS source_url,
    poems.license_name AS license_name,
    poems.license_info AS license_info,
    poems.rights_verification_status AS rights_verification_status,
    poems.commercial_use_sweden AS commercial_use_sweden,
    poems.imported_at AS imported_at
"""


def poem_exists(connection: sqlite3.Connection, source_url: str, title: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM poems WHERE source_url = ? AND title = ?",
        (source_url, title),
    ).fetchone()
    return row is not None


def insert_poem(
    connection: sqlite3.Connection,
    draft: PoemDraft,
    *,
    collection_id: str,
    imported_at: str,
) -> int:
    translator = _blank(draft.translator)
    cursor = connection.execute(
        """
        INSERT INTO poems (
            collection_id, title, author, author_death_year, author_death_year_source,
            author_death_year_verified, origin, origin_source, original_language,
            text_language, translator, translator_death_year, translator_death_year_source,
            translator_death_year_verified, body, source_url, license_name, license_info,
            rights_verification_status, commercial_use_sweden, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            collection_id,
            draft.title,
            draft.author,
            draft.author_death_year,
            _blank(draft.author_death_year_source),
            1 if _blank(draft.author_death_year_source) else 0,
            _blank(draft.origin),
            _blank(draft.origin_source),
            draft.original_language.strip(),
            draft.text_language.strip(),
            translator,
            draft.translator_death_year if translator else None,
            _blank(draft.translator_death_year_source) if translator else None,
            1 if translator and _blank(draft.translator_death_year_source) else 0,
            draft.body,
            draft.source_url.strip(),
            draft.license_name.strip(),
            draft.license_info.strip(),
            draft.rights_verification_status,
            "yes",
            imported_at,
        ),
    )
    return int(cursor.lastrowid)


def list_eligible(connection: sqlite3.Connection) -> list[Poem]:
    rows = connection.execute(
        f"""
        SELECT {POEM_COLUMNS}
        FROM poems
        WHERE commercial_use_sweden = 'yes'
          AND rights_verification_status = 'documented'
          AND NOT EXISTS (
              SELECT 1 FROM selection_history
              WHERE selection_history.poem_id = poems.id
                AND selection_history.status = 'confirmed'
          )
        ORDER BY poems.id
        """
    ).fetchall()
    return [_poem_from_row(row) for row in rows]


def get_poem(connection: sqlite3.Connection, poem_id: int) -> Poem | None:
    row = connection.execute(
        f"SELECT {POEM_COLUMNS} FROM poems WHERE id = ?",
        (poem_id,),
    ).fetchone()
    if row is None:
        return None
    return _poem_from_row(row)


def get_pending(connection: sqlite3.Connection) -> Selection | None:
    row = connection.execute(
        f"""
        SELECT selection_history.id AS selection_id,
               selection_history.selected_at,
               selection_history.confirmed_at,
               selection_history.skipped_at,
               selection_history.status,
               {POEM_COLUMNS}
        FROM selection_history
        JOIN poems ON poems.id = selection_history.poem_id
        WHERE selection_history.status = 'pending'
        """
    ).fetchone()
    if row is None:
        return None
    return _selection_from_row(row, newly_pending=False)


def insert_pending(connection: sqlite3.Connection, poem: Poem, *, selected_at: str) -> Selection:
    cursor = connection.execute(
        """
        INSERT INTO selection_history (poem_id, selected_at, status)
        VALUES (?, ?, 'pending')
        """,
        (poem.id, selected_at),
    )
    return Selection(
        id=int(cursor.lastrowid),
        poem=poem,
        selected_at=selected_at,
        status="pending",
        confirmed_at=None,
        skipped_at=None,
        newly_pending=True,
    )


def resolve_pending(connection: sqlite3.Connection, *, status: str, at: str) -> Selection | None:
    pending = get_pending(connection)
    if pending is None:
        return None
    if status == "confirmed":
        connection.execute(
            """
            UPDATE selection_history
            SET status = 'confirmed', confirmed_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (at, pending.id),
        )
        return Selection(
            id=pending.id,
            poem=pending.poem,
            selected_at=pending.selected_at,
            status="confirmed",
            confirmed_at=at,
            skipped_at=None,
        )
    connection.execute(
        """
        UPDATE selection_history
        SET status = 'skipped', skipped_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (at, pending.id),
    )
    return Selection(
        id=pending.id,
        poem=pending.poem,
        selected_at=pending.selected_at,
        status="skipped",
        confirmed_at=None,
        skipped_at=at,
    )


def count_poems(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM poems").fetchone()[0])


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _blank(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _poem_from_row(row: sqlite3.Row) -> Poem:
    return Poem(
        id=row["id"],
        collection_id=row["collection_id"],
        title=row["title"],
        author=row["author"],
        author_death_year=row["author_death_year"],
        author_death_year_source=row["author_death_year_source"],
        author_death_year_verified=bool(row["author_death_year_verified"]),
        origin=row["origin"],
        origin_source=row["origin_source"],
        original_language=row["original_language"],
        text_language=row["text_language"],
        translator=row["translator"],
        translator_death_year=row["translator_death_year"],
        translator_death_year_source=row["translator_death_year_source"],
        translator_death_year_verified=bool(row["translator_death_year_verified"]),
        body=row["body"],
        source_url=row["source_url"],
        license_name=row["license_name"],
        license_info=row["license_info"],
        rights_verification_status=row["rights_verification_status"],
        commercial_use_sweden=row["commercial_use_sweden"],
        imported_at=row["imported_at"],
    )


def _selection_from_row(row: sqlite3.Row, *, newly_pending: bool) -> Selection:
    poem = Poem(
        id=row["id"],
        collection_id=row["collection_id"],
        title=row["title"],
        author=row["author"],
        author_death_year=row["author_death_year"],
        author_death_year_source=row["author_death_year_source"],
        author_death_year_verified=bool(row["author_death_year_verified"]),
        origin=row["origin"],
        origin_source=row["origin_source"],
        original_language=row["original_language"],
        text_language=row["text_language"],
        translator=row["translator"],
        translator_death_year=row["translator_death_year"],
        translator_death_year_source=row["translator_death_year_source"],
        translator_death_year_verified=bool(row["translator_death_year_verified"]),
        body=row["body"],
        source_url=row["source_url"],
        license_name=row["license_name"],
        license_info=row["license_info"],
        rights_verification_status=row["rights_verification_status"],
        commercial_use_sweden=row["commercial_use_sweden"],
        imported_at=row["imported_at"],
    )
    return Selection(
        id=row["selection_id"],
        poem=poem,
        selected_at=row["selected_at"],
        status=row["status"],
        confirmed_at=row["confirmed_at"],
        skipped_at=row["skipped_at"],
        newly_pending=newly_pending,
    )
