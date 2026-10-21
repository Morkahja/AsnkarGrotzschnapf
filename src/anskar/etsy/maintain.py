"""Sales sync, 30-day discount, 42-day expiry, and retry of failed daily listings."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anskar.etsy.client import EtsyClient, results_list
from anskar.etsy.credentials import ensure_fresh_credentials
from anskar.etsy.errors import EtsyError
from anskar.etsy.pricing import DISCOUNT_AFTER_DAYS, OFFERING_DAYS
from anskar.etsy.state import ETSY_LISTING_QUEUED, etsy_is_ready, load_etsy_state
from anskar.etsy.store import (
    active_listings,
    connect,
    etsy_database_path,
    find_by_listing_id,
    list_offerings,
    record_sale,
    update_listing,
)
from anskar.printmaker import packs_dir

TRANSACTION_PAGE = 100
TRANSACTION_PAGE_CAP = 50  # hard safety cap: at most 5000 transactions per run


def maintain_etsy(
    *,
    database: Path | None = None,
    client: EtsyClient | None = None,
    now: datetime | None = None,
    offline: bool = False,
    packs: Path | None = None,
) -> dict[str, object]:
    """
    Reconcile sales, apply the day-30 discount, remove unsold listings after 42 days,
    and publish packs that a prior daily run left retry-queued (with an error).

    Safe to run while seller verification is pending (no writes). Independent of daily painting.
    Catch-up: running this after sleep/power-off still applies overdue 42-day expiry.
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    report: dict[str, object] = {
        "ran_at": clock.isoformat(),
        "seller_state": load_etsy_state(),
        "seller_ready": etsy_is_ready(),
        "sales_recorded": 0,
        "discounted": 0,
        "expired": 0,
        "removed": 0,
        "retries_published": 0,
        "skipped": [],
        "errors": [],
        "offline_delay": False,
    }
    if not etsy_is_ready():
        report["skipped"].append("seller_not_ready")  # type: ignore[union-attr]
        return report
    if offline:
        report["offline_delay"] = True
        report["errors"].append(  # type: ignore[union-attr]
            "Computer offline; deadlines will catch up on the next successful run."
        )
        return report

    try:
        if client is None:
            creds = ensure_fresh_credentials()
            graph = EtsyClient(creds)
        else:
            graph = client
    except EtsyError as exc:
        report["errors"].append(str(exc))  # type: ignore[union-attr]
        report["offline_delay"] = exc.kind in {"timeout", "api"}
        return report

    db = database or etsy_database_path()
    try:
        sales = _sync_sales(graph, database=db, now=clock)
        report["sales_recorded"] = sales
    except EtsyError as exc:
        report["errors"].append(f"sales: {exc}")  # type: ignore[union-attr]
        if exc.kind == "timeout":
            report["offline_delay"] = True
            return report

    try:
        discounted, expired, removed = _apply_schedule(graph, database=db, now=clock)
        report["discounted"] = discounted
        report["expired"] = expired
        report["removed"] = removed
    except EtsyError as exc:
        report["errors"].append(f"schedule: {exc}")  # type: ignore[union-attr]
        if exc.kind == "timeout":
            report["offline_delay"] = True

    try:
        retries = _retry_failed_daily_packs(graph, packs=packs or packs_dir(), database=db)
        report["retries_published"] = retries
    except EtsyError as exc:
        report["errors"].append(f"retry: {exc}")  # type: ignore[union-attr]
        if exc.kind == "timeout":
            report["offline_delay"] = True
    return report


def offerings_status(*, database: Path | None = None, limit: int = 50) -> dict[str, object]:
    with connect(database or etsy_database_path()) as connection:
        rows = list_offerings(connection, limit=limit)
        from anskar.etsy.store import completed_sales_count

        sales = completed_sales_count(connection)
    items = []
    for row in rows:
        items.append(
            {
                "pack_id": row["pack_id"],
                "artwork_id": row["artwork_id"],
                "listing_id": row["listing_id"],
                "listing_url": row["listing_url"],
                "state": row["state"],
                "price_r": row["price_r"],
                "price_s": row["price_s"],
                "original_price_sek": row["original_price_sek"],
                "current_price_sek": row["current_price_sek"],
                "currency": row["currency"],
                "discount_applied": bool(row["discount_applied"]),
                "activated_at": row["activated_at"],
                "discounted_at": row["discounted_at"],
                "sold_at": row["sold_at"],
                "expired_at": row["expired_at"],
                "etsy_transaction_id": row["etsy_transaction_id"],
            }
        )
    return {
        "completed_sales_count": sales,
        "offerings": items,
        "seller_state": load_etsy_state(),
    }


def _sync_sales(client: EtsyClient, *, database: Path, now: datetime) -> int:
    recorded = 0
    transactions = _collect_transactions(client)

    with connect(database) as connection:
        for item in transactions:
            if not _transaction_qualifies(item):
                continue
            transaction_id = str(item.get("transaction_id") or item.get("id") or "")
            listing_id = str(item.get("listing_id") or "")
            if not transaction_id or not listing_id:
                continue
            receipt_id = str(
                item.get("receipt_id") or item.get("_receipt_id") or item.get("receipt") or ""
            ) or None
            row = find_by_listing_id(connection, listing_id)
            artwork_id = str(row["artwork_id"]) if row is not None else None
            pack_id = str(row["pack_id"]) if row is not None else None
            if not record_sale(
                connection,
                transaction_id=transaction_id,
                receipt_id=receipt_id,
                listing_id=listing_id,
                artwork_id=artwork_id,
                pack_id=pack_id,
            ):
                continue
            recorded += 1
            if row is not None and row["state"] == "active":
                update_listing(
                    connection,
                    int(row["id"]),
                    state="sold",
                    sold_at=now.isoformat(),
                    etsy_transaction_id=transaction_id,
                    current_price_sek=row["current_price_sek"],
                )
    return recorded


def _collect_transactions(client: EtsyClient) -> list[dict]:
    transactions: list[dict] = []
    try:
        for page in range(TRANSACTION_PAGE_CAP):
            payload = client.get_shop_transactions(
                limit=TRANSACTION_PAGE,
                offset=page * TRANSACTION_PAGE,
            )
            batch = results_list(payload, "results")
            if not batch:
                break
            transactions.extend(batch)
            if len(batch) < TRANSACTION_PAGE:
                break
        return transactions
    except EtsyError:
        pass

    # Receipt fallback with the same hard page cap.
    for page in range(TRANSACTION_PAGE_CAP):
        try:
            receipts = results_list(
                client.get_shop_receipts(
                    was_paid=True,
                    limit=TRANSACTION_PAGE,
                    offset=page * TRANSACTION_PAGE,
                ),
                "results",
            )
        except EtsyError:
            break
        if not receipts:
            break
        for receipt in receipts:
            receipt_id = str(receipt.get("receipt_id") or receipt.get("id") or "")
            if not receipt_id:
                continue
            if receipt.get("was_paid") is False or receipt.get("is_canceled") or receipt.get("was_canceled"):
                continue
            try:
                tx_payload = client.get_shop_receipt_transactions(receipt_id)
            except EtsyError:
                continue
            for item in results_list(tx_payload, "results"):
                item = dict(item)
                item["_receipt_id"] = receipt_id
                transactions.append(item)
        if len(receipts) < TRANSACTION_PAGE:
            break
    return transactions


def _apply_schedule(client: EtsyClient, *, database: Path, now: datetime) -> tuple[int, int, int]:
    discounted = 0
    expired = 0
    removed = 0
    with connect(database) as connection:
        rows = list(active_listings(connection))
    for row in rows:
        listing_id = str(row["listing_id"] or "")
        if not listing_id:
            continue
        activated = _parse_time(row["activated_at"]) or _parse_time(row["created_at"])
        if activated is None:
            continue
        age = now - activated
        # Reconcile remote removal before any local sale/expiry action.
        try:
            remote = client.get_listing(listing_id)
        except EtsyError as exc:
            if exc.kind == "timeout":
                raise
            continue
        remote_state = str(remote.get("state") or "")
        quantity = remote.get("quantity")
        if remote_state in {"sold_out", "remove", "removed"} or quantity == 0:
            # Not a confirmed paid sale — do not increment the sales counter.
            with connect(database) as connection:
                update_listing(
                    connection,
                    int(row["id"]),
                    state="removed",
                    expired_at=now.isoformat(),
                )
            removed += 1
            continue

        if age >= timedelta(days=OFFERING_DAYS):
            try:
                client.delete_listing(listing_id)
            except EtsyError as exc:
                if "404" not in str(exc):
                    raise
            with connect(database) as connection:
                update_listing(
                    connection,
                    int(row["id"]),
                    state="expired",
                    expired_at=now.isoformat(),
                )
            expired += 1
            continue

        if age >= timedelta(days=DISCOUNT_AFTER_DAYS) and not row["discount_applied"]:
            from anskar.etsy.pricing import ArtworkPrice
            from anskar.etsy.listing import listing_fields, load_pack
            from anskar.printmaker import packs_dir as default_packs

            price = ArtworkPrice(
                r=int(row["price_r"] or 0),
                s=int(row["price_s"] or 0),
                original_sek=int(round(float(row["original_price_sek"] or 0))),
                currency=str(row["currency"] or "SEK"),
            )
            new_inclusive = price.discounted_sek
            client.update_listing_price(listing_id, price.as_api_price(discounted=True))
            # Refresh description so a stale "Listed price" line is not left behind.
            try:
                folder, manifest = load_pack(str(row["pack_id"]), packs=default_packs())
                fields = listing_fields(manifest, price_sek=new_inclusive)
                client.update_listing(
                    listing_id,
                    {
                        "description": fields["description"],
                        "who_made": fields["who_made"],
                        "when_made": fields["when_made"],
                        "is_supply": False,
                    },
                )
            except (EtsyError, OSError, TypeError, ValueError):
                pass
            with connect(database) as connection:
                update_listing(
                    connection,
                    int(row["id"]),
                    discount_applied=1,
                    discounted_at=now.isoformat(),
                    current_price_sek=float(new_inclusive),
                )
            discounted += 1
    return discounted, expired, removed


def _retry_failed_daily_packs(
    client: EtsyClient,
    *,
    packs: Path,
    database: Path,
) -> int:
    """Publish only packs a prior daily attempt left as queued_for_publish with an error."""
    from anskar.etsy.service import publish_pack

    published = 0
    if not packs.is_dir():
        return 0
    for folder in sorted(packs.iterdir()):
        if not folder.is_dir():
            continue
        queue_path = folder / "etsy" / "queue.json"
        if not queue_path.is_file():
            continue
        try:
            payload = json.loads(queue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("status") != ETSY_LISTING_QUEUED:
            continue
        if not payload.get("error"):
            # Queued without error is pending-verification backlog — never bulk-publish.
            continue
        pack_id = folder.name
        try:
            publish_pack(pack_id, dry_run=False, packs=packs, database=database, client=client)
        except EtsyError:
            continue
        published += 1
    return published


def _transaction_qualifies(item: dict) -> bool:
    if item.get("is_canceled") or item.get("was_canceled"):
        return False
    paid = item.get("was_paid")
    if paid is False:
        return False
    return True


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
