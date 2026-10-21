"""Sales sync, 30-day discount, and 42-day expiry for Etsy offerings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from anskar.etsy.client import EtsyClient, results_list
from anskar.etsy.credentials import ensure_fresh_credentials
from anskar.etsy.errors import EtsyError
from anskar.etsy.pricing import DISCOUNT_AFTER_DAYS, OFFERING_DAYS
from anskar.etsy.state import etsy_is_ready, load_etsy_state
from anskar.etsy.store import (
    active_listings,
    connect,
    etsy_database_path,
    find_by_listing_id,
    list_offerings,
    record_sale,
    update_listing,
)


def maintain_etsy(
    *,
    database: Path | None = None,
    client: EtsyClient | None = None,
    now: datetime | None = None,
    offline: bool = False,
) -> dict[str, object]:
    """
    Reconcile sales, apply the day-30 discount, and remove unsold listings after 42 days.

    Safe to run while seller verification is pending (no writes). Independent of daily painting.
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
        "skipped": [],
        "errors": [],
        "offline_delay": False,
    }
    if not etsy_is_ready():
        report["skipped"].append("seller_not_ready")  # type: ignore[union-attr]
        return report
    if offline:
        report["offline_delay"] = True
        report["errors"].append("Computer offline; deadlines will catch up on the next successful run.")  # type: ignore[union-attr]
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
        discounted, expired = _apply_schedule(graph, database=db, now=clock)
        report["discounted"] = discounted
        report["expired"] = expired
    except EtsyError as exc:
        report["errors"].append(f"schedule: {exc}")  # type: ignore[union-attr]
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
    # Prefer shop transactions; fall back to paid receipts.
    transactions: list[dict] = []
    try:
        payload = client.get_shop_transactions(limit=100, offset=0)
        transactions = results_list(payload, "results")
    except EtsyError:
        receipts = results_list(client.get_shop_receipts(was_paid=True, limit=50), "results")
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


def _apply_schedule(client: EtsyClient, *, database: Path, now: datetime) -> tuple[int, int]:
    discounted = 0
    expired = 0
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
        # Reconcile sale status before any removal.
        try:
            remote = client.get_listing(listing_id)
        except EtsyError as exc:
            if exc.kind == "timeout":
                raise
            continue
        remote_state = str(remote.get("state") or "")
        quantity = remote.get("quantity")
        if remote_state in {"sold_out", "remove", "removed"} or quantity == 0:
            with connect(database) as connection:
                update_listing(
                    connection,
                    int(row["id"]),
                    state="sold",
                    sold_at=now.isoformat(),
                )
            continue

        if age >= timedelta(days=OFFERING_DAYS):
            # Final sale check via transactions already ran; delete unsold listing.
            try:
                client.delete_listing(listing_id)
            except EtsyError as exc:
                # If already gone, still mark expired locally.
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

            price = ArtworkPrice(
                r=int(row["price_r"] or 0),
                s=int(row["price_s"] or 0),
                original_sek=int(round(float(row["original_price_sek"] or 0))),
                currency=str(row["currency"] or "SEK"),
            )
            # Store inclusive target; send net to Etsy so Swedish VAT display stays whole.
            new_inclusive = price.discounted_sek
            client.update_listing_price(listing_id, price.as_api_price(discounted=True))
            with connect(database) as connection:
                update_listing(
                    connection,
                    int(row["id"]),
                    discount_applied=1,
                    discounted_at=now.isoformat(),
                    current_price_sek=float(new_inclusive),
                )
            discounted += 1
    return discounted, expired


def _transaction_qualifies(item: dict) -> bool:
    if item.get("is_canceled") or item.get("was_canceled"):
        return False
    paid = item.get("was_paid")
    if paid is False:
        return False
    # Receipt-level unpaid should already be filtered; treat missing as paid when listed.
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
