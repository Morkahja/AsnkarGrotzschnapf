"""Orchestrate poem → image → pack → Instagram → (queue or publish Etsy)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from anskar.archive.models import ArchiveError, NoEligiblePoemError
from anskar.archive.selection import confirm_pending, select_poem, skip_pending
from anskar.artwork.generator import ArtworkRecord, GenerationError, generate_pending
from anskar.artwork.settings import load_settings
from anskar.config import PROJECT_ROOT, database_path as archive_database_path
from anskar.daily.direction import (
    PRODUCTION_ABSTRACTION,
    assert_usable_surreal_prompt,
    reinforce_production_prompt,
)
from anskar.daily.errors import DailyError
from anskar.daily.ledger import (
    ACTIVE,
    begin_run,
    connect,
    database_path,
    get_run,
    list_runs as ledger_list,
    public_run,
    update_run,
)
from anskar.daily.lock import exclusive_lock
from anskar.daily.notify import notify_stage
from anskar.archive.repository import get_poem, insert_pending, utc_now
from anskar.daily.recovery import (
    MAX_SELECTION_ATTEMPTS,
    SelectionDecision,
    decide_for_pending,
    find_resumable_unused,
    poems_with_succeeded_artwork,
)
from anskar.db import connect as archive_connect
from anskar.etsy.errors import EtsyError
from anskar.etsy.service import publish_pack as etsy_publish
from anskar.etsy.state import (
    ETSY_LISTING_PENDING,
    ETSY_LISTING_PUBLISHED,
    ETSY_LISTING_QUEUED,
    etsy_is_ready,
    load_etsy_state,
    public_etsy_state,
)
from anskar.instagram.errors import InstagramError
from anskar.instagram.service import publish as instagram_publish
from anskar.printmaker import PrintError, approve_artwork, artwork_id, packs_dir
from anskar.style import production_style

GenerateFn = Callable[..., ArtworkRecord]
PackFn = Callable[..., dict]
InstagramFn = Callable[..., dict]
EtsyFn = Callable[..., dict]


def daily_run(
    *,
    dry_run: bool = False,
    archive_db: Path | None = None,
    daily_db: Path | None = None,
    packs: Path | None = None,
    artworks: Path | None = None,
    generate: GenerateFn | None = None,
    prepare_pack: PackFn | None = None,
    publish_instagram: InstagramFn | None = None,
    publish_etsy: EtsyFn | None = None,
    lock: Path | None = None,
    allow_test: bool = False,
    queue_etsy_only: bool = False,
) -> dict[str, object]:
    """Select, generate, pack, publish Instagram; queue or publish Etsy. Auto-approves."""
    with exclusive_lock(lock):
        return _run_or_resume(
            run_id=None,
            dry_run=dry_run,
            archive_db=archive_db,
            daily_db=daily_db,
            packs=packs,
            artworks=artworks,
            generate=generate,
            prepare_pack=prepare_pack,
            publish_instagram=publish_instagram,
            publish_etsy=publish_etsy,
            allow_test=allow_test,
            queue_etsy_only=queue_etsy_only,
        )


def resume_run(
    run_id: str,
    *,
    archive_db: Path | None = None,
    daily_db: Path | None = None,
    packs: Path | None = None,
    artworks: Path | None = None,
    generate: GenerateFn | None = None,
    prepare_pack: PackFn | None = None,
    publish_instagram: InstagramFn | None = None,
    publish_etsy: EtsyFn | None = None,
    lock: Path | None = None,
) -> dict[str, object]:
    with exclusive_lock(lock):
        return _run_or_resume(
            run_id=run_id,
            dry_run=None,
            archive_db=archive_db,
            daily_db=daily_db,
            packs=packs,
            artworks=artworks,
            generate=generate,
            prepare_pack=prepare_pack,
            publish_instagram=publish_instagram,
            publish_etsy=publish_etsy,
            queue_etsy_only=False,  # stored flag on the run wins below
        )


def run_status(*, daily_db: Path | None = None) -> dict[str, object]:
    with connect(daily_db) as connection:
        active = connection.execute(
            f"""
            SELECT * FROM daily_runs
            WHERE status IN ({",".join("?" for _ in ACTIVE)})
            ORDER BY created_at DESC LIMIT 1
            """,
            ACTIVE,
        ).fetchone()
        latest = connection.execute(
            "SELECT * FROM daily_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    etsy = public_etsy_state()
    return {
        "active": public_run(active) if active is not None else None,
        "latest": public_run(latest) if latest is not None else None,
        "etsy_integration": etsy,
        "etsy_configured_credentials": False,  # credentials optional while pending
        "auto_approval": True,
        "poem_marked_used_after_instagram": True,
        "etsy_blocks_daily_completion": False,
    }


def list_runs(*, daily_db: Path | None = None, limit: int = 20) -> list[dict[str, object]]:
    with connect(daily_db) as connection:
        return [public_run(row) for row in ledger_list(connection, limit=limit)]


def _run_or_resume(
    *,
    run_id: str | None,
    dry_run: bool | None,
    archive_db: Path | None,
    daily_db: Path | None,
    packs: Path | None,
    artworks: Path | None,
    generate: GenerateFn | None,
    prepare_pack: PackFn | None,
    publish_instagram: InstagramFn | None,
    publish_etsy: EtsyFn | None,
    allow_test: bool = False,
    queue_etsy_only: bool = False,
) -> dict[str, object]:
    archive = archive_db or archive_database_path()
    daily = daily_db or database_path()
    pack_root = packs or packs_dir()
    artwork_root = artworks or (PROJECT_ROOT / "data" / "artworks")

    with connect(daily) as connection:
        if run_id is None:
            active_id = begin_run(
                connection,
                dry_run=bool(dry_run),
                run_kind="test" if allow_test else "daily",
                queue_etsy_only=queue_etsy_only,
            )
            row = get_run(connection, active_id)
            assert row is not None
            is_dry = bool(row["dry_run"]) if dry_run is None else bool(dry_run)
        else:
            row = get_run(connection, run_id)
            if row is None:
                raise DailyError(f"Daily run not found: {run_id}", kind="error")
            if row["status"] == "completed":
                return public_run(row)
            if row["status"] == "failed":
                if row["pack_id"]:
                    update_run(connection, run_id, status="pack_prepared")
                    row = get_run(connection, run_id)
                elif row["image_path"]:
                    # Pack failed after a successful generation — resume from the image.
                    update_run(connection, run_id, status="generated")
                    row = get_run(connection, run_id)
                else:
                    raise DailyError(
                        f"Daily run {run_id} failed before an artwork existed and cannot be resumed.",
                        kind="error",
                    )
            assert row is not None
            active_id = run_id
            is_dry = bool(row["dry_run"]) if dry_run is None else bool(dry_run)

    # Prefer the persisted rehearsal/queue flag over the caller default on resume.
    keys = row.keys()
    if "queue_etsy_only" in keys and row["queue_etsy_only"] is not None:
        queue_etsy_only = bool(row["queue_etsy_only"]) or queue_etsy_only

    try:
        status = str(row["status"])
        if status == "selected":
            notify_stage("start", _run_detail(row, prefix="Starting"))
        else:
            notify_stage("start", _run_detail(row, prefix="Resuming"))
        if status == "selected":
            _step_select(
                active_id,
                archive=archive,
                daily=daily,
                packs=pack_root,
            )
            with connect(daily) as connection:
                current = get_run(connection, active_id)
            if current is not None and current["image_path"]:
                status = "generated"
            else:
                _step_generate(
                    active_id,
                    archive=archive,
                    daily=daily,
                    artwork_root=artwork_root,
                    generate=generate,
                )
                status = "generated"
            with connect(daily) as connection:
                current = get_run(connection, active_id)
            if current is not None and current["pack_id"]:
                status = "pack_prepared"
            else:
                _step_pack(
                    active_id,
                    archive=archive,
                    daily=daily,
                    packs=pack_root,
                    artwork_root=artwork_root,
                    prepare_pack=prepare_pack,
                )
                status = "pack_prepared"
        elif status == "generated":
            _step_pack(
                active_id,
                archive=archive,
                daily=daily,
                packs=pack_root,
                artwork_root=artwork_root,
                prepare_pack=prepare_pack,
            )
            status = "pack_prepared"
        elif status == "pack_prepared":
            pass
        elif status == "instagram_published":
            # Legacy incomplete rows: finish poem mark + etsy queue + complete.
            _complete_after_instagram(
                active_id,
                archive=archive,
                daily=daily,
                packs=pack_root,
                dry_run=is_dry,
                publish_etsy=publish_etsy,
                queue_etsy_only=queue_etsy_only,
            )
            finished = _public(daily, active_id)
            notify_stage("complete", _public_detail(finished))
            return finished
        else:
            raise DailyError(f"Cannot resume from status {status}.", kind="error")

        with connect(daily) as connection:
            row = get_run(connection, active_id)
            assert row is not None
            pack_id = str(row["pack_id"] or "")
            status = str(row["status"])
            already_ig = bool(row["instagram_media_id"]) or (
                "instagram_status" in row.keys() and row["instagram_status"] == "published"
            )

        if status == "pack_prepared" and not already_ig:
            _step_instagram(
                active_id,
                pack_id=pack_id,
                daily=daily,
                dry_run=is_dry,
                publish_instagram=publish_instagram,
            )

        _complete_after_instagram(
            active_id,
            archive=archive,
            daily=daily,
            packs=pack_root,
            dry_run=is_dry,
            publish_etsy=publish_etsy,
            queue_etsy_only=queue_etsy_only,
        )
        finished = _public(daily, active_id)
        notify_stage("complete", _public_detail(finished))
        return finished
    except (DailyError, ArchiveError, GenerationError, PrintError, InstagramError, EtsyError) as exc:
        kind = str(getattr(exc, "kind", "error"))
        with connect(daily) as connection:
            current = get_run(connection, active_id)
            # Soft Instagram media failures: count up to 3, keep poem eligible, stay resumable
            # until the third permanent media error, then fail the run without skipping the poem.
            if (
                kind == "media"
                and isinstance(exc, InstagramError)
                and current is not None
                and current["status"] == "pack_prepared"
            ):
                fails = int(current["media_fail_count"] or 0) + 1 if "media_fail_count" in current.keys() else 1
                if fails < 3:
                    update_run(
                        connection,
                        active_id,
                        media_fail_count=fails,
                        error_kind=kind,
                        error_message=str(exc),
                    )
                else:
                    update_run(
                        connection,
                        active_id,
                        status="failed",
                        media_fail_count=fails,
                        error_kind=kind,
                        error_message=str(exc),
                    )
            elif (
                current is not None
                and current["status"] in {"pack_prepared", "instagram_published"}
                and kind not in {"empty", "duplicate"}
            ):
                # Transient API/timeout: keep status so noon can auto-resume.
                update_run(
                    connection,
                    active_id,
                    error_kind=kind,
                    error_message=str(exc),
                )
            else:
                update_run(
                    connection,
                    active_id,
                    status="failed",
                    error_kind=kind,
                    error_message=str(exc),
                )
        notify_stage("failed", str(exc))
        if isinstance(exc, DailyError):
            raise
        raise DailyError(str(exc), kind=kind) from exc


def _complete_after_instagram(
    run_id: str,
    *,
    archive: Path,
    daily: Path,
    packs: Path,
    dry_run: bool,
    publish_etsy: EtsyFn | None,
    queue_etsy_only: bool = False,
) -> None:
    with connect(daily) as connection:
        row = get_run(connection, run_id)
        assert row is not None
        pack_id = str(row["pack_id"] or "")

    # Mark poem used as soon as Instagram has succeeded (or dry-run staged).
    if not dry_run:
        _mark_poem_used_after_instagram(run_id, archive=archive, daily=daily)
    else:
        with connect(daily) as connection:
            update_run(connection, run_id, instagram_status="dry_run")

    _queue_or_publish_etsy(
        run_id,
        pack_id=pack_id,
        packs=packs,
        daily=daily,
        dry_run=dry_run,
        publish_etsy=publish_etsy,
        queue_etsy_only=queue_etsy_only,
    )

    with connect(daily) as connection:
        # Clear run-level transient errors only. Pack etsy_last_error stays for maintain retry.
        update_run(
            connection,
            run_id,
            status="completed",
            completed_at=_iso_now(),
            error_kind=None,
            error_message=None,
        )
    if pack_id and not dry_run:
        _mark_pack_instagram_published(packs / pack_id)


def _step_select(run_id: str, *, archive: Path, daily: Path, packs: Path) -> None:
    """Select an eligible poem, resume compatible artwork, or skip conflicts."""
    with connect(daily) as connection:
        current = get_run(connection, run_id)
        if current is not None and current["image_path"]:
            return

    selection_log: list[dict[str, object]] = []
    with archive_connect(archive) as connection:
        exclude = set(poems_with_succeeded_artwork(connection))

    for _attempt in range(MAX_SELECTION_ATTEMPTS):
        try:
            with archive_connect(archive) as connection:
                pending_row = connection.execute(
                    "SELECT poem_id FROM selection_history WHERE status = 'pending'"
                ).fetchone()
                if pending_row is None:
                    # Prefer recovering an unused poem that already has production artwork.
                    published_ids = {
                        poem_id
                        for poem_id in exclude
                        if _poem_already_on_instagram(
                            poem_id, archive=archive, daily_db=daily
                        )
                    }
                    recovered = find_resumable_unused(
                        connection,
                        packs=packs,
                        already_published=published_ids,
                    )
                    if recovered is not None and recovered.poem_id is not None:
                        poem = get_poem(connection, recovered.poem_id)
                        if poem is None:
                            raise DailyError(
                                f"Resumable poem {recovered.poem_id} is missing from the archive.",
                                kind="error",
                            )
                        selection = insert_pending(
                            connection, poem, selected_at=utc_now()
                        )
                        decision = SelectionDecision(
                            action="resume",
                            poem_id=selection.poem.id,
                            selection_id=selection.id,
                            title=selection.poem.title,
                            author=selection.poem.author,
                            artwork=recovered.artwork,
                            pack_id=recovered.pack_id,
                            note=recovered.note,
                        )
                        already_ig = False
                    else:
                        selection = select_poem(
                            connection,
                            exclude_poem_ids=exclude,
                        )
                        already_ig = _poem_already_on_instagram(
                            selection.poem.id, archive=archive, daily_db=daily
                        )
                        decision = decide_for_pending(
                            connection,
                            poem_id=selection.poem.id,
                            selection_id=selection.id,
                            title=selection.poem.title,
                            author=selection.poem.author,
                            packs=packs,
                            already_on_instagram=already_ig,
                        )
                else:
                    # Do not exclude the current pending poem: may resume it.
                    pending_ids = {int(pending_row[0])}
                    selection = select_poem(
                        connection,
                        exclude_poem_ids=exclude - pending_ids,
                    )
                    already_ig = _poem_already_on_instagram(
                        selection.poem.id, archive=archive, daily_db=daily
                    )
                    decision = decide_for_pending(
                        connection,
                        poem_id=selection.poem.id,
                        selection_id=selection.id,
                        title=selection.poem.title,
                        author=selection.poem.author,
                        packs=packs,
                        already_on_instagram=already_ig,
                    )
        except NoEligiblePoemError as exc:
            with connect(daily) as connection:
                update_run(
                    connection,
                    run_id,
                    selection_log=selection_log,
                    status="failed",
                    error_kind="empty",
                    error_message=str(exc),
                )
            raise DailyError(str(exc), kind="empty") from exc

        entry = {
            "action": decision.action,
            "poem_id": decision.poem_id,
            "selection_id": decision.selection_id,
            "title": decision.title,
            "note": decision.note,
            "pack_id": decision.pack_id,
            "artwork_path": str(decision.artwork.path) if decision.artwork else None,
        }
        selection_log.append(entry)

        if decision.action == "generate":
            with connect(daily) as connection:
                update_run(
                    connection,
                    run_id,
                    status="selected",
                    poem_id=selection.poem.id,
                    selection_id=selection.id,
                    title=selection.poem.title,
                    author=selection.poem.author,
                    artwork_status="selected",
                    instagram_status="pending",
                    etsy_status=ETSY_LISTING_PENDING,
                    selection_log=selection_log,
                    error_kind=None,
                    error_message=None,
                )
            return

        if decision.action == "resume":
            assert decision.artwork is not None
            artwork = decision.artwork
            image = artwork.path
            digest = (
                hashlib.sha256(image.read_bytes()).hexdigest() if image.is_file() else None
            )
            fields: dict[str, object] = {
                "poem_id": selection.poem.id,
                "selection_id": selection.id,
                "title": selection.poem.title,
                "author": selection.poem.author,
                "artwork_db_id": artwork.artwork_db_id,
                "image_path": str(image),
                "image_sha256": digest,
                "seed": artwork.seed,
                "prompt_sha256": artwork.prompt_sha256,
                "visual_prompt": artwork.visual_prompt,
                "model_id": artwork.model_id or "z-image-turbo",
                "settings_json": artwork.settings_json,
                "artwork_status": "generated",
                "instagram_status": "pending",
                "etsy_status": ETSY_LISTING_PENDING,
                "selection_log": selection_log,
                "error_kind": None,
                "error_message": None,
            }
            if decision.pack_id:
                fields["status"] = "pack_prepared"
                fields["pack_id"] = decision.pack_id
            else:
                fields["status"] = "generated"
            with connect(daily) as connection:
                update_run(connection, run_id, **fields)
            return

        # skip_legacy: dismiss conflict and try another poem.
        with archive_connect(archive) as connection:
            if already_ig:
                confirm_pending(connection)
            else:
                skip_pending(connection)
            exclude = set(poems_with_succeeded_artwork(connection))
            if decision.poem_id is not None:
                exclude.add(decision.poem_id)

    with connect(daily) as connection:
        update_run(
            connection,
            run_id,
            selection_log=selection_log,
            status="failed",
            error_kind="empty",
            error_message=(
                f"No eligible poem found after {MAX_SELECTION_ATTEMPTS} selection attempts."
            ),
        )
    raise DailyError(
        f"No eligible poem found after {MAX_SELECTION_ATTEMPTS} selection attempts.",
        kind="empty",
    )


def _step_generate(
    run_id: str,
    *,
    archive: Path,
    daily: Path,
    artwork_root: Path,
    generate: GenerateFn | None,
) -> None:
    from dataclasses import replace

    with connect(daily) as connection:
        row = get_run(connection, run_id)
    notify_stage("generate", _run_detail(row) if row is not None else "")
    settings = load_settings()
    settings = replace(settings, artworks_dir=artwork_root)
    style = production_style()  # painterly + wrapped
    runner = generate or generate_pending
    kwargs = {
        "style": style,
        "abstraction": PRODUCTION_ABSTRACTION,
        "model_id": "z-image-turbo",
        "width": 1600,
        "height": 1600,
        "prompt_transform": reinforce_production_prompt,
        "prompt_validate": assert_usable_surreal_prompt,
    }
    try:
        record = runner(archive, settings, **kwargs)
    except TypeError as exc:
        # Only fall back when the callable rejects unexpected keyword arguments.
        message = str(exc)
        if "unexpected keyword" not in message and "got an unexpected" not in message:
            raise
        record = runner(
            archive,
            settings,
            style=style,
            abstraction=PRODUCTION_ABSTRACTION,
            model_id="z-image-turbo",
            width=1600,
            height=1600,
        )
    image = Path(record.output_path)
    digest = hashlib.sha256(image.read_bytes()).hexdigest() if image.is_file() else None
    sidecar = image.with_suffix(".json")
    visual = ""
    prompt_sha = record.prompt_sha256
    model_id = "z-image-turbo"
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if isinstance(meta, dict):
            visual = str(meta.get("visual_prompt") or meta.get("prompt") or "")
            model_id = str(meta.get("model_id") or model_id)
    source_digest = None
    try:
        saved = json.loads(record.settings_json)
        source = saved.get("source_text") if isinstance(saved, dict) else None
        if isinstance(source, str) and source:
            source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    except (TypeError, json.JSONDecodeError):
        source_digest = None
    with connect(daily) as connection:
        update_run(
            connection,
            run_id,
            status="generated",
            artwork_status="generated",
            artwork_db_id=record.id,
            image_path=str(image),
            image_sha256=digest,
            seed=record.seed,
            prompt_sha256=prompt_sha,
            visual_prompt=visual,
            model_id=model_id,
            duration_seconds=record.duration_seconds,
            settings_json=record.settings_json,
            source_sha256=source_digest,
        )


def _step_pack(
    run_id: str,
    *,
    archive: Path,
    daily: Path,
    packs: Path,
    artwork_root: Path,
    prepare_pack: PackFn | None,
) -> None:
    with connect(daily) as connection:
        row = get_run(connection, run_id)
        assert row is not None
        image_path = Path(str(row["image_path"]))
        existing_pack_id = str(row["pack_id"] or "") if row["pack_id"] else ""
    metadata = image_path.with_suffix(".json")
    if not metadata.is_file():
        raise DailyError("Artwork metadata is missing after generation.", kind="media")

    # Reuse a valid pack already prepared for this artwork (resume after rights repair).
    reused = _reusable_pack(metadata, packs=packs, preferred_id=existing_pack_id or None)
    if reused is not None:
        pack_id, manifest = reused
    else:
        maker = prepare_pack or approve_artwork
        try:
            manifest = maker(
                metadata,
                experiments=artwork_root,
                database=archive,
                packs=packs,
                artworks=artwork_root,
            )
        except PrintError as exc:
            # Ready pack already on disk from a prior repair — attach it.
            reused = _reusable_pack(metadata, packs=packs, preferred_id=None)
            if reused is None:
                raise DailyError(str(exc), kind="media") from exc
            pack_id, manifest = reused
        else:
            pack_id = str(manifest.get("artwork_id") or "")

    if not manifest.get("publication_ready"):
        raise DailyError(
            "The production pack is not publication-ready. "
            + str((manifest.get("rights") or {}).get("reason") or "Rights or print checks failed."),
            kind="media",
        )
    if not pack_id:
        pack_id = str(manifest.get("artwork_id") or "")
    _write_etsy_queue_marker(packs / pack_id, status=ETSY_LISTING_PENDING)
    with connect(daily) as connection:
        update_run(
            connection,
            run_id,
            status="pack_prepared",
            pack_id=pack_id,
            artwork_status="generated",
            etsy_status=ETSY_LISTING_PENDING,
        )


def _reusable_pack(
    metadata: Path,
    *,
    packs: Path,
    preferred_id: str | None,
) -> tuple[str, dict] | None:
    """Return (pack_id, manifest) when a publication-ready pack for this artwork exists."""
    try:
        record = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    candidates: list[str] = []
    if preferred_id:
        candidates.append(preferred_id)
    expected = artwork_id(record)
    if expected not in candidates:
        candidates.append(expected)
    poem_id = record.get("poem_id")
    seed = record.get("seed")
    for pack_id in candidates:
        manifest_path = packs / pack_id / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        if not manifest.get("publication_ready"):
            continue
        # Do not reattach a pack already published unless finishing that same run.
        if manifest.get("instagram_published") or manifest.get("etsy_published") or manifest.get(
            "published"
        ):
            continue
        rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
        pack_poem = rights.get("poem_id")
        if pack_poem is None:
            pack_poem = manifest.get("poem_id")
        if poem_id is not None and pack_poem is not None and pack_poem != poem_id:
            continue
        if seed is not None and manifest.get("seed") is not None and manifest.get("seed") != seed:
            continue
        reused = dict(manifest)
        reused["artwork_id"] = pack_id
        reused["pack_path"] = str(packs / pack_id)
        return pack_id, reused
    return None


def _step_instagram(
    run_id: str,
    *,
    pack_id: str,
    daily: Path,
    dry_run: bool,
    publish_instagram: InstagramFn | None,
) -> None:
    with connect(daily) as connection:
        row = get_run(connection, run_id)
    notify_stage(
        "instagram",
        _run_detail(row, extra=f"Pack {pack_id}") if row is not None else f"Pack {pack_id}",
    )
    publisher = publish_instagram or instagram_publish
    if dry_run:
        result = publisher(pack_id, dry_run=True)
        media_id = None
        permalink = None
        ig_status = "dry_run"
    else:
        result = publisher(pack_id, confirm=True)
        media_id = result.get("media_id") if isinstance(result.get("media_id"), str) else None
        permalink = result.get("permalink") if isinstance(result.get("permalink"), str) else None
        ig_status = "published"
    with connect(daily) as connection:
        update_run(
            connection,
            run_id,
            status="instagram_published",
            instagram_status=ig_status,
            instagram_media_id=media_id,
            instagram_permalink=permalink,
            error_kind=None,
            error_message=None,
        )


def _queue_or_publish_etsy(
    run_id: str,
    *,
    pack_id: str,
    packs: Path,
    daily: Path,
    dry_run: bool,
    publish_etsy: EtsyFn | None,
    queue_etsy_only: bool = False,
) -> None:
    """While Etsy is pending verification, only queue. Never fail the daily run for Etsy."""
    if not pack_id:
        with connect(daily) as connection:
            update_run(connection, run_id, etsy_status=ETSY_LISTING_PENDING)
        return

    if queue_etsy_only or not etsy_is_ready():
        notify_stage("etsy_queued", f"Pack {pack_id}")
        _write_etsy_queue_marker(packs / pack_id, status=ETSY_LISTING_PENDING)
        with connect(daily) as connection:
            update_run(
                connection,
                run_id,
                etsy_status=ETSY_LISTING_PENDING,
            )
        return

    # Ready: publish for this new run only. Backlog still needs an explicit command.
    if dry_run:
        notify_stage("etsy", f"Dry run for pack {pack_id}")
        if publish_etsy is not None:
            publish_etsy(pack_id, dry_run=True, packs=packs)
        else:
            try:
                etsy_publish(pack_id, dry_run=True, packs=packs)
            except EtsyError:
                pass
        _write_etsy_queue_marker(packs / pack_id, status=ETSY_LISTING_QUEUED)
        with connect(daily) as connection:
            update_run(connection, run_id, etsy_status=ETSY_LISTING_QUEUED)
        return

    notify_stage("etsy", f"Pack {pack_id}")
    publisher = publish_etsy or etsy_publish
    try:
        result = publisher(pack_id, dry_run=False, packs=packs)
    except EtsyError as exc:
        _write_etsy_queue_marker(
            packs / pack_id,
            status=ETSY_LISTING_QUEUED,
            error=str(exc),
        )
        with connect(daily) as connection:
            update_run(
                connection,
                run_id,
                etsy_status=ETSY_LISTING_QUEUED,
                error_kind=exc.kind,
                error_message=f"Instagram completed; Etsy retry queued: {exc}",
            )
        return
    with connect(daily) as connection:
        update_run(
            connection,
            run_id,
            etsy_status=ETSY_LISTING_PUBLISHED,
            etsy_listing_id=result.get("listing_id") if isinstance(result.get("listing_id"), str) else None,
            etsy_listing_url=result.get("listing_url")
            if isinstance(result.get("listing_url"), str)
            else None,
        )
    _write_etsy_queue_marker(
        packs / pack_id,
        status=ETSY_LISTING_PUBLISHED,
        listing_id=result.get("listing_id") if isinstance(result.get("listing_id"), str) else None,
        listing_url=result.get("listing_url") if isinstance(result.get("listing_url"), str) else None,
    )


def _mark_poem_used_after_instagram(run_id: str, *, archive: Path, daily: Path) -> None:
    """Mark the run's poem used. A different pending selection is left alone."""
    with connect(daily) as connection:
        row = get_run(connection, run_id)
        assert row is not None
    if row["poem_id"] is None:
        return
    poem_id = int(row["poem_id"])
    selection_id = row["selection_id"]
    with archive_connect(archive) as connection:
        pending = connection.execute(
            "SELECT id, poem_id FROM selection_history WHERE status = 'pending'"
        ).fetchone()
        if pending is not None and int(pending["poem_id"]) == poem_id:
            confirm_pending(connection)
            return
        if selection_id is None:
            return
        connection.execute(
            """
            UPDATE selection_history
            SET status = 'confirmed', confirmed_at = ?, skipped_at = NULL
            WHERE id = ? AND poem_id = ? AND status != 'confirmed'
            """,
            (utc_now(), int(selection_id), poem_id),
        )


def _poem_already_on_instagram(
    poem_id: int,
    *,
    archive: Path,
    daily_db: Path | None = None,
) -> bool:
    """Detect a prior Instagram publication for this poem."""
    try:
        with connect(daily_db) as connection:
            hit = connection.execute(
                """
                SELECT 1 FROM daily_runs
                WHERE poem_id = ?
                  AND instagram_status = 'published'
                  AND dry_run = 0
                LIMIT 1
                """,
                (poem_id,),
            ).fetchone()
        if hit is not None:
            return True
    except Exception:  # noqa: BLE001
        pass
    # Consult the live Instagram ledger only for the production archive.
    if archive.resolve() != archive_database_path().resolve():
        return False
    from anskar.instagram.ledger import connect as ig_connect
    from anskar.instagram.service import database_path as ig_db

    try:
        with ig_connect(ig_db()) as connection:
            rows = connection.execute(
                "SELECT pack_id FROM attempts WHERE state = 'published'"
            ).fetchall()
    except Exception:  # noqa: BLE001
        return False
    root = packs_dir()
    for row in rows:
        pack_id = row["pack_id"]
        manifest_path = root / str(pack_id) / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rights = payload.get("rights") if isinstance(payload, dict) else None
        if isinstance(rights, dict) and rights.get("poem_id") == poem_id:
            return True
        if isinstance(payload, dict) and payload.get("poem_id") == poem_id:
            return True
    return False


def _write_etsy_queue_marker(
    folder: Path,
    *,
    status: str,
    listing_id: str | None = None,
    listing_url: str | None = None,
    error: str | None = None,
) -> None:
    if not folder.is_dir():
        return
    manifest_path = folder / "manifest.json"
    payload: dict | None = None
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            payload = None
    if payload is not None:
        payload["etsy_status"] = status
        payload["etsy_integration_state"] = load_etsy_state()
        if listing_id:
            payload["etsy_listing_id"] = listing_id
        if listing_url:
            payload["etsy_listing_url"] = listing_url
        if error:
            payload["etsy_last_error"] = error
        if status == ETSY_LISTING_PENDING:
            payload["etsy_published"] = False
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    # Always write queue.json; never overwrite a corrupt/non-dict manifest with {}.
    queue = folder / "etsy" / "queue.json"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        json.dumps(
            {
                "status": status,
                "integration_state": load_etsy_state(),
                "listing_id": listing_id,
                "listing_url": listing_url,
                "error": error,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _mark_pack_instagram_published(folder: Path) -> None:
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    payload["instagram_published"] = True
    payload["daily_auto_approved"] = True
    # Overall pack "published" means Instagram day completed; Etsy may still be pending.
    payload["published"] = True
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _public(daily: Path, run_id: str) -> dict[str, object]:
    with connect(daily) as connection:
        row = get_run(connection, run_id)
    assert row is not None
    return public_run(row)


def _run_detail(row, *, prefix: str = "", extra: str = "") -> str:
    """Short human detail for a notification body from a ledger row."""
    parts: list[str] = []
    if prefix:
        parts.append(prefix.rstrip(".") + ".")
    title = ""
    if hasattr(row, "keys") and "poem_title" in row.keys() and row["poem_title"]:
        title = str(row["poem_title"])
    elif isinstance(row, dict) and row.get("poem_title"):
        title = str(row["poem_title"])
    if title:
        parts.append(title)
    poem_id = None
    if hasattr(row, "keys") and "poem_id" in row.keys():
        poem_id = row["poem_id"]
    elif isinstance(row, dict):
        poem_id = row.get("poem_id")
    if poem_id is not None and not title:
        parts.append(f"Poem {poem_id}")
    if extra:
        parts.append(extra)
    return " ".join(parts).strip()


def _public_detail(payload: dict[str, object]) -> str:
    title = str(payload.get("poem_title") or "").strip()
    pack = str(payload.get("pack_id") or "").strip()
    etsy = str(payload.get("etsy_status") or "").strip()
    bits = [bit for bit in (title, pack and f"pack {pack}", etsy and f"Etsy {etsy}") if bit]
    return " · ".join(bits)


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
