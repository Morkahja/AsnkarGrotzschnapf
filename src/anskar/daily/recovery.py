"""Resolve pending poems that already have production artwork."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from anskar.artwork.png import png_size
from anskar.printmaker import artwork_id as pack_artwork_id

PRODUCTION_WIDTH = 1600
PRODUCTION_HEIGHT = 1600
MAX_SELECTION_ATTEMPTS = 8


@dataclass(frozen=True)
class ExistingArtwork:
    artwork_db_id: int
    poem_id: int
    selection_id: int
    path: Path
    metadata_path: Path
    seed: int
    width: int
    height: int
    prompt_sha256: str
    model_id: str | None
    visual_prompt: str
    settings_json: str
    meets_production: bool
    reason: str


@dataclass(frozen=True)
class SelectionDecision:
    action: str  # generate | resume | skip_legacy | empty
    poem_id: int | None = None
    selection_id: int | None = None
    title: str | None = None
    author: str | None = None
    artwork: ExistingArtwork | None = None
    pack_id: str | None = None
    note: str = ""


def succeeded_artwork(connection: sqlite3.Connection, poem_id: int) -> ExistingArtwork | None:
    row = connection.execute(
        """
        SELECT id, poem_id, selection_id, seed, width, height, output_path,
               prompt_sha256, settings_json, status
        FROM artworks
        WHERE poem_id = ? AND status = 'succeeded'
        ORDER BY id DESC LIMIT 1
        """,
        (poem_id,),
    ).fetchone()
    if row is None:
        return None
    path = Path(str(row["output_path"]))
    metadata_path = path.with_suffix(".json")
    visual = ""
    model_id = None
    experiment = None
    if metadata_path.is_file():
        try:
            meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if isinstance(meta, dict):
            visual = str(meta.get("visual_prompt") or meta.get("prompt") or "")
            model_id = str(meta.get("model_id") or "") or None
            experiment = meta.get("experiment")
    meets, reason = _meets_production(
        path,
        width=int(row["width"]),
        height=int(row["height"]),
        experiment=str(experiment) if experiment else None,
    )
    return ExistingArtwork(
        artwork_db_id=int(row["id"]),
        poem_id=int(row["poem_id"]),
        selection_id=int(row["selection_id"]),
        path=path,
        metadata_path=metadata_path,
        seed=int(row["seed"]),
        width=int(row["width"]),
        height=int(row["height"]),
        prompt_sha256=str(row["prompt_sha256"]),
        model_id=model_id,
        visual_prompt=visual,
        settings_json=str(row["settings_json"] or "{}"),
        meets_production=meets,
        reason=reason,
    )


def poems_with_succeeded_artwork(connection: sqlite3.Connection) -> set[int]:
    rows = connection.execute(
        "SELECT DISTINCT poem_id FROM artworks WHERE status = 'succeeded'"
    ).fetchall()
    return {int(row[0]) for row in rows}


def _pack_is_usable(manifest_path: Path) -> bool:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("instagram_published") or payload.get("published"):
        return False
    return bool(payload.get("publication_ready"))


def find_existing_pack(packs: Path, artwork: ExistingArtwork) -> str | None:
    """Return pack_id if a publication-ready, unpublished pack for this artwork exists."""
    if artwork.metadata_path.is_file():
        try:
            record = json.loads(artwork.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = {}
        if isinstance(record, dict):
            candidate = pack_artwork_id(record)
            manifest = packs / candidate / "manifest.json"
            if manifest.is_file() and _pack_is_usable(manifest):
                return candidate
    # Fallback: production-poem-{id}-seed-{seed}-*
    prefix = f"production-poem-{artwork.poem_id}-seed-{artwork.seed}-"
    if not packs.is_dir():
        return None
    for folder in sorted(packs.iterdir()):
        manifest = folder / "manifest.json"
        if folder.name.startswith(prefix) and manifest.is_file() and _pack_is_usable(manifest):
            return folder.name
    # Any pack whose manifest points at this poem and seed.
    for folder in sorted(packs.iterdir()):
        manifest_path = folder / "manifest.json"
        if not manifest_path.is_file() or not _pack_is_usable(manifest_path):
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        rights = payload.get("rights") if isinstance(payload.get("rights"), dict) else {}
        if rights.get("poem_id") == artwork.poem_id and payload.get("seed") == artwork.seed:
            return folder.name
    return None


def find_resumable_unused(
    connection: sqlite3.Connection,
    *,
    packs: Path,
    already_published: set[int],
) -> SelectionDecision | None:
    """Unused poem with a compatible production artwork that was never published."""
    rows = connection.execute(
        """
        SELECT poems.id AS poem_id, poems.title, poems.author
        FROM poems
        WHERE commercial_use_sweden = 'yes'
          AND rights_verification_status = 'documented'
          AND NOT EXISTS (
              SELECT 1 FROM selection_history
              WHERE selection_history.poem_id = poems.id
                AND selection_history.status = 'confirmed'
          )
          AND EXISTS (
              SELECT 1 FROM artworks
              WHERE artworks.poem_id = poems.id
                AND artworks.status = 'succeeded'
          )
        ORDER BY poems.id
        """
    ).fetchall()
    for row in rows:
        poem_id = int(row["poem_id"])
        if poem_id in already_published:
            continue
        existing = succeeded_artwork(connection, poem_id)
        if existing is None or not existing.meets_production:
            continue
        pack_id = find_existing_pack(packs, existing)
        note = f"Recovering unpublished production artwork {existing.path.name}"
        if pack_id:
            note = f"{note} with pack {pack_id}."
        else:
            note = f"{note}; the print pack will be prepared from that file."
        return SelectionDecision(
            action="resume",
            poem_id=poem_id,
            selection_id=existing.selection_id,
            title=str(row["title"]),
            author=str(row["author"]),
            artwork=existing,
            pack_id=pack_id,
            note=note,
        )
    return None


def decide_for_pending(
    connection: sqlite3.Connection,
    *,
    poem_id: int,
    selection_id: int,
    title: str,
    author: str,
    packs: Path,
    already_on_instagram: bool,
) -> SelectionDecision:
    if already_on_instagram:
        return SelectionDecision(
            action="skip_legacy",
            poem_id=poem_id,
            selection_id=selection_id,
            title=title,
            author=author,
            note="Poem already published on Instagram; dismissing pending selection.",
        )
    existing = succeeded_artwork(connection, poem_id)
    if existing is None:
        return SelectionDecision(
            action="generate",
            poem_id=poem_id,
            selection_id=selection_id,
            title=title,
            author=author,
            note="Pending poem has no succeeded artwork; generate a new one.",
        )
    if not existing.meets_production:
        return SelectionDecision(
            action="skip_legacy",
            poem_id=poem_id,
            selection_id=selection_id,
            title=title,
            author=author,
            artwork=existing,
            note=(
                f"Preserved incompatible artwork at {existing.path.name} "
                f"({existing.reason}); selecting another poem."
            ),
        )
    pack_id = find_existing_pack(packs, existing)
    return SelectionDecision(
        action="resume",
        poem_id=poem_id,
        selection_id=selection_id,
        title=title,
        author=author,
        artwork=existing,
        pack_id=pack_id,
        note=(
            f"Resuming existing production artwork {existing.path.name}"
            + (f" with pack {pack_id}." if pack_id else " (pack will be prepared).")
        ),
    )


def _meets_production(
    path: Path,
    *,
    width: int,
    height: int,
    experiment: str | None,
) -> tuple[bool, str]:
    if not path.is_file():
        return False, "artwork file missing"
    try:
        measured = png_size(path)
    except (OSError, ValueError):
        return False, "unreadable PNG"
    if measured != (PRODUCTION_WIDTH, PRODUCTION_HEIGHT):
        return False, f"size {measured[0]}×{measured[1]} (need {PRODUCTION_WIDTH}×{PRODUCTION_HEIGHT})"
    if (width, height) != (PRODUCTION_WIDTH, PRODUCTION_HEIGHT):
        return False, f"recorded size {width}×{height}"
    # Prompt Lab and other non-production experiments never count as daily masters.
    if experiment is not None and experiment != "production":
        return False, f"non-production experiment ({experiment})"
    return True, "production 1600×1600"
