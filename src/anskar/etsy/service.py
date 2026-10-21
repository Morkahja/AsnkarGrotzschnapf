"""Publish one pack to Etsy as a single-sale digital download."""

from __future__ import annotations

import json
from pathlib import Path

from anskar.etsy.client import EtsyClient, listing_url, results_list
from anskar.etsy.credentials import EtsyCredentials, ensure_fresh_credentials, etsy_configured
from anskar.etsy.errors import EtsyError
from anskar.etsy.attributes import apply_listing_attributes, print_inches_from_manifest
from anskar.etsy.gallery import ensure_gallery_details
from anskar.etsy.listing import assert_etsy_ready, digital_files, listing_fields, load_pack, preview_image
from anskar.etsy.pricing import (
    REQUIRED_CURRENCY,
    VAT_INCLUSIVE_NOTE,
    ArtworkPrice,
    assert_shop_currency,
    calculate_price,
)
from anskar.etsy.state import etsy_is_ready
from anskar.etsy.store import (
    completed_sales_count,
    connect,
    etsy_database_path,
    find_by_artwork,
    insert_listing,
    update_listing,
)
from anskar.printmaker import packs_dir

__all__ = ["etsy_configured", "align_listing", "publish_pack", "published_listing"]


def align_listing(
    pack_id: str,
    listing_id: str,
    *,
    packs: Path | None = None,
    credentials: EtsyCredentials | None = None,
    client: EtsyClient | None = None,
    rng=None,
    price_sek: float | None = None,
) -> dict[str, object]:
    """Rewrite an existing live listing to match the current pack listing format."""
    root = packs or packs_dir()
    folder, manifest = load_pack(pack_id, packs=root)
    assert_etsy_ready(manifest, folder)
    if price_sek is None:
        price_sek = float(manifest.get("etsy_original_price_sek") or 0) or None
    if price_sek is None:
        raise EtsyError("Cannot align listing without a known SEK price.", kind="media")
    fields = listing_fields(manifest, price_sek=price_sek)
    if not etsy_is_ready():
        raise EtsyError(
            "Etsy seller state is not ready. Listings stay queued until "
            "`anskar etsy set-state ready` after seller verification.",
            kind="credentials",
        )
    stored = credentials or ensure_fresh_credentials()
    graph = client or EtsyClient(stored)
    shop = graph.get_shop(stored.shop_id)
    assert_shop_currency(str(shop.get("currency_code") or shop.get("currency") or ""))
    graph.update_listing(
        listing_id,
        {
            "title": fields["title"],
            "description": fields["description"],
            "tags": fields["tags"],
            "materials": fields["materials"],
            "who_made": fields["who_made"],
            "when_made": fields["when_made"],
            "is_supply": False,
            "type": "download",
            "quantity": 1,
            "should_auto_renew": False,
        },
    )
    gallery = ensure_gallery_details(folder, rng=rng)
    _sync_listing_gallery(graph, listing_id, gallery)
    apply_listing_attributes(
        graph,
        listing_id,
        print_inches=print_inches_from_manifest(manifest),
        square=str(manifest.get("orientation") or "square").lower() == "square",
    )
    existing_files = results_list(graph.get_listing_files(listing_id), "results")
    if not existing_files:
        for path in digital_files(folder):
            graph.upload_listing_file(listing_id, path)
    _assert_assets_attached(graph, listing_id, expected_images=len(gallery))
    url = listing_url(listing_id)
    from datetime import datetime, timezone

    activated = datetime.now(timezone.utc).isoformat()
    price = ArtworkPrice(
        r=int(manifest.get("etsy_price_r") or 0),
        s=int(manifest.get("etsy_price_s") or 0),
        original_sek=int(price_sek),
        currency=REQUIRED_CURRENCY,
    )
    _mark_pack_etsy(
        folder,
        listing_id=listing_id,
        listing_url=url,
        price=price,
        activated_at=str(manifest.get("etsy_activated_at") or activated),
    )
    return {
        "aligned": True,
        "pack_id": pack_id,
        "listing_id": listing_id,
        "listing_url": url,
        "title": fields["title"],
        "images": len(gallery),
        "price_sek": int(price_sek),
    }


def publish_pack(
    pack_id: str,
    *,
    dry_run: bool = False,
    packs: Path | None = None,
    database: Path | None = None,
    credentials: EtsyCredentials | None = None,
    client: EtsyClient | None = None,
    rng=None,
) -> dict[str, object]:
    root = packs or packs_dir()
    folder, manifest = load_pack(pack_id, packs=root)
    assert_etsy_ready(manifest, folder)
    artwork_id = str(manifest.get("artwork_id") or pack_id)
    db = database or etsy_database_path()

    with connect(db) as connection:
        terminal = find_by_artwork(
            connection,
            artwork_id,
            states=("active", "sold", "expired"),
        )
        if terminal is not None and terminal["state"] in {"sold", "expired"}:
            raise EtsyError(
                f"Artwork {artwork_id} already ended as {terminal['state']} and will not be relisted.",
                kind="duplicate",
            )
        if terminal is not None and terminal["state"] == "active":
            raise EtsyError("This artwork already has an active Etsy listing.", kind="duplicate")
        resumable = find_by_artwork(
            connection,
            artwork_id,
            states=("prepared", "draft_created", "assets_uploaded"),
        )
        sales_before = completed_sales_count(connection)
        if resumable is not None and resumable["original_price_sek"] is not None:
            price = ArtworkPrice(
                r=int(resumable["price_r"] or 0),
                s=int(resumable["price_s"] or 0),
                original_sek=int(resumable["original_price_sek"]),
                currency=str(resumable["currency"] or REQUIRED_CURRENCY),
            )
            attempt_id = int(resumable["id"])
            listing_id = str(resumable["listing_id"] or "")
            resume_state = str(resumable["state"])
        else:
            price = calculate_price(sales_before=sales_before, rng=rng)
            attempt_id = None
            listing_id = ""
            resume_state = ""

    fields = listing_fields(manifest, price_sek=price.original_sek)
    fields["price"] = price.as_api_price()
    fields["quantity"] = 1
    fields["should_auto_renew"] = False

    if dry_run:
        digital_files(folder)
        preview_image(folder)
        gallery = ensure_gallery_details(folder, rng=rng)
        with connect(db) as connection:
            insert_listing(
                connection,
                pack_id=pack_id,
                artwork_id=artwork_id,
                state="dry_run",
                price_r=price.r,
                price_s=price.s,
                original_price_sek=float(price.original_sek),
                current_price_sek=float(price.original_sek),
                currency=REQUIRED_CURRENCY,
                quantity=1,
            )
        return {
            "dry_run": True,
            "published": False,
            "pack_id": pack_id,
            "title": fields["title"],
            "tags": fields["tags"],
            "quantity": 1,
            "price_sek": price.original_sek,
            "price_r": price.r,
            "price_s": price.s,
            "currency": REQUIRED_CURRENCY,
            "price_includes_vat": True,
            "vat_note": VAT_INCLUSIVE_NOTE,
            "gallery_images": [path.name for path in gallery],
            "digital_files": [path.name for path in digital_files(folder)],
        }

    if not etsy_is_ready():
        raise EtsyError(
            "Etsy seller state is not ready. Listings stay queued until "
            "`anskar etsy set-state ready` after seller verification.",
            kind="credentials",
        )

    stored = credentials or ensure_fresh_credentials()
    graph = client or EtsyClient(stored)
    shop = graph.get_shop(stored.shop_id)
    assert_shop_currency(str(shop.get("currency_code") or shop.get("currency") or ""))

    with connect(db) as connection:
        if attempt_id is None:
            attempt_id = insert_listing(
                connection,
                pack_id=pack_id,
                artwork_id=artwork_id,
                state="prepared",
                price_r=price.r,
                price_s=price.s,
                original_price_sek=float(price.original_sek),
                current_price_sek=float(price.original_sek),
                currency=REQUIRED_CURRENCY,
                quantity=1,
            )
            resume_state = "prepared"

    try:
        if resume_state in {"", "prepared"} or not listing_id:
            draft = graph.create_draft_listing(fields)
            listing_id = str(draft.get("listing_id") or draft.get("id") or "")
            if not listing_id:
                raise EtsyError("Etsy did not return a listing id.", kind="api")
            with connect(db) as connection:
                update_listing(
                    connection,
                    attempt_id,
                    state="draft_created",
                    listing_id=listing_id,
                    listing_url=listing_url(listing_id),
                )
            resume_state = "draft_created"

        if resume_state == "draft_created":
            gallery = ensure_gallery_details(folder, rng=rng)
            _sync_listing_gallery(graph, listing_id, gallery)
            existing_files = results_list(graph.get_listing_files(listing_id), "results")
            if not existing_files:
                for path in digital_files(folder):
                    graph.upload_listing_file(listing_id, path)
            _assert_assets_attached(graph, listing_id, expected_images=len(gallery))
            apply_listing_attributes(
                graph,
                listing_id,
                print_inches=print_inches_from_manifest(manifest),
                square=str(manifest.get("orientation") or "square").lower() == "square",
            )
            with connect(db) as connection:
                update_listing(connection, attempt_id, state="assets_uploaded")
            resume_state = "assets_uploaded"

        if resume_state == "assets_uploaded":
            published = graph.publish_listing(listing_id)
            remote_state = str(published.get("state") or "active")
            url = listing_url(listing_id)
            from datetime import datetime, timezone

            activated = datetime.now(timezone.utc).isoformat()
            with connect(db) as connection:
                update_listing(
                    connection,
                    attempt_id,
                    state="active",
                    listing_id=listing_id,
                    listing_url=url,
                    activated_at=activated,
                    current_price_sek=float(price.original_sek),
                )
            _mark_pack_etsy(
                folder,
                listing_id=listing_id,
                listing_url=url,
                price=price,
                activated_at=activated,
            )
            return {
                "dry_run": False,
                "published": True,
                "pack_id": pack_id,
                "artwork_id": artwork_id,
                "listing_id": listing_id,
                "listing_url": url,
                "state": remote_state,
                "title": fields["title"],
                "quantity": 1,
                "price_sek": price.original_sek,
                "price_r": price.r,
                "price_s": price.s,
                "currency": REQUIRED_CURRENCY,
                "activated_at": activated,
            }
        raise EtsyError(f"Cannot resume Etsy publish from state {resume_state!r}.", kind="api")
    except EtsyError as exc:
        with connect(db) as connection:
            # Keep draft/assets progress so a timeout cannot create a second listing.
            if listing_id:
                update_listing(
                    connection,
                    attempt_id,
                    listing_id=listing_id,
                    error_kind=exc.kind,
                    error_message=str(exc),
                )
            else:
                update_listing(
                    connection,
                    attempt_id,
                    state="failed",
                    error_kind=exc.kind,
                    error_message=str(exc),
                )
        raise


def published_listing(artwork_id: str, *, database: Path | None = None) -> dict[str, object] | None:
    with connect(database or etsy_database_path()) as connection:
        row = find_by_artwork(
            connection,
            artwork_id,
            states=("active", "sold", "expired"),
        )
    if row is None:
        return None
    return {
        "pack_id": row["pack_id"],
        "artwork_id": row["artwork_id"],
        "listing_id": row["listing_id"],
        "listing_url": row["listing_url"],
        "state": row["state"],
        "original_price_sek": row["original_price_sek"],
        "current_price_sek": row["current_price_sek"],
        "activated_at": row["activated_at"],
        "discount_applied": bool(row["discount_applied"]),
    }


def _sync_listing_gallery(client: EtsyClient, listing_id: str, gallery: list[Path]) -> None:
    """Attach exactly the gallery images, replacing duplicates from a partial resume."""
    existing = results_list(client.get_listing_images(listing_id), "results")
    if len(existing) == len(gallery):
        return
    old_ids = [
        image.get("listing_image_id") or image.get("image_id")
        for image in existing
    ]
    # Etsy refuses to delete the final image, so upload the intended set first.
    for rank, path in enumerate(gallery, start=1):
        client.upload_listing_image(listing_id, path, rank=rank)
    for image_id in old_ids:
        if image_id is None:
            continue
        client.delete_listing_image(listing_id, image_id)


def _assert_assets_attached(
    client: EtsyClient,
    listing_id: str,
    *,
    expected_images: int | None = None,
) -> None:
    images = results_list(client.get_listing_images(listing_id), "results")
    files = results_list(client.get_listing_files(listing_id), "results")
    if not images:
        raise EtsyError("Etsy listing has no preview image attached.", kind="api")
    if expected_images is not None and len(images) != expected_images:
        raise EtsyError(
            f"Etsy listing has {len(images)} images; expected {expected_images} "
            "(full artwork plus detail crops).",
            kind="api",
        )
    if not files:
        raise EtsyError("Etsy listing has no downloadable print file attached.", kind="api")


def _mark_pack_etsy(
    folder: Path,
    *,
    listing_id: str,
    listing_url: str,
    price: ArtworkPrice,
    activated_at: str,
) -> None:
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
    payload["etsy_status"] = "published"
    payload["etsy_price_r"] = price.r
    payload["etsy_price_s"] = price.s
    payload["etsy_original_price_sek"] = price.original_sek
    payload["etsy_currency"] = price.currency
    payload["etsy_activated_at"] = activated_at
    payload["etsy_quantity"] = 1
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    queue_path = folder / "etsy" / "queue.json"
    queue_path.write_text(
        json.dumps(
            {
                "status": "published",
                "integration_state": "ready",
                "listing_id": listing_id,
                "listing_url": listing_url,
                "error": None,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
