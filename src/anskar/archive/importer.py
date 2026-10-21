"""Import a structured poetry collection into SQLite."""

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from anskar.archive.eligibility import assess
from anskar.archive.models import ImportReport, PoemDraft
from anskar.archive.repository import insert_poem, poem_exists

class CollectionFormatError(Exception):
    """The collection file is not a usable document."""


def import_collection(
    connection: sqlite3.Connection,
    path: Path,
    *,
    on: date | None = None,
    imported_at: str | None = None,
) -> ImportReport:
    document = _load(path)
    collection_id = document.get("collection_id")
    if not isinstance(collection_id, str) or not collection_id.strip():
        raise CollectionFormatError("collection_id is missing")
    poems = document.get("poems")
    if not isinstance(poems, list):
        raise CollectionFormatError("poems must be a list")

    imported = 0
    duplicates = 0
    rejected: list[tuple[str, str]] = []
    stamp = imported_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    assessment_date = on or date.today()

    for index, entry in enumerate(poems, start=1):
        label = f"poem {index}"
        if not isinstance(entry, dict):
            rejected.append((label, "entry is not an object"))
            continue
        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            label = title.strip()
        try:
            draft = _draft_from_entry(entry)
        except ValueError as exc:
            rejected.append((label, str(exc)))
            continue
        decision = assess(draft, on=assessment_date)
        if not decision.acceptable:
            rejected.append((label, "; ".join(decision.reasons)))
            continue
        if poem_exists(connection, draft.source_url.strip(), draft.title):
            duplicates += 1
            continue
        insert_poem(connection, draft, collection_id=collection_id.strip(), imported_at=stamp)
        imported += 1

    return ImportReport(imported, duplicates, tuple(rejected))


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CollectionFormatError(f"collection is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CollectionFormatError("collection must be a JSON object")
    return data


def _draft_from_entry(entry: dict) -> PoemDraft:
    return PoemDraft(
        title=_text(entry, "title"),
        author=_text(entry, "author"),
        author_death_year=_year(entry.get("author_death_year"), "author_death_year"),
        author_death_year_source=_optional_text(entry.get("author_death_year_source")),
        origin=_optional_text(entry.get("origin")),
        origin_source=_optional_text(entry.get("origin_source")),
        original_language=_text(entry, "original_language"),
        text_language=_text(entry, "text_language"),
        translator=_optional_text(entry.get("translator")),
        translator_death_year=_year(entry.get("translator_death_year"), "translator_death_year"),
        translator_death_year_source=_optional_text(entry.get("translator_death_year_source")),
        body=_body(entry.get("body")),
        source_url=_text(entry, "source_url"),
        license_name=_text(entry, "license_name"),
        license_info=_text(entry, "license_info"),
        rights_verification_status=_text(entry, "rights_verification_status"),
    )


def _text(entry: dict, key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional text field must be a string or null")
    stripped = value.strip()
    return stripped or None


def _body(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("body must be a string")
    return value


def _year(value: object, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer year or null")
    return value
