"""Pre-flight checks before live Etsy listing writes."""

from __future__ import annotations

from pathlib import Path

from anskar.etsy.client import EtsyClient
from anskar.etsy.credentials import (
    ensure_fresh_credentials,
    etsy_configured,
    shop_id_matches,
    shop_name_matches,
)
from anskar.etsy.errors import EtsyError
from anskar.etsy.listing import assert_etsy_ready, digital_files, load_pack, preview_image
from anskar.etsy.oauth import EXPECTED_SHOP_ID, EXPECTED_SHOP_NAME
from anskar.etsy.pricing import REQUIRED_CURRENCY, assert_shop_currency
from anskar.etsy.state import etsy_is_ready, load_etsy_state
from anskar.printmaker import packs_dir


def etsy_publish_readiness(
    pack_id: str | None = None,
    *,
    packs: Path | None = None,
    client: EtsyClient | None = None,
) -> dict[str, object]:
    """
    Check shop identity, seller state, permissions material, assets, and SEK currency.

    Does not create listings. Does not change seller verification state.
    """
    report: dict[str, object] = {
        "ready_to_publish": False,
        "seller_state": load_etsy_state(),
        "seller_ready": etsy_is_ready(),
        "oauth_configured": etsy_configured(),
        "expected_shop_id": EXPECTED_SHOP_ID,
        "expected_shop_name": EXPECTED_SHOP_NAME,
        "checks": {},
    }
    checks: dict[str, object] = report["checks"]  # type: ignore[assignment]
    if not report["seller_ready"]:
        checks["seller_verification"] = "blocked: state is not ready"
        return report
    checks["seller_verification"] = "ok"
    if not report["oauth_configured"]:
        checks["oauth"] = "blocked: OAuth credentials missing"
        return report
    checks["oauth"] = "ok"
    try:
        creds = ensure_fresh_credentials()
    except EtsyError as exc:
        checks["oauth"] = f"blocked: {exc}"
        return report
    graph = client or EtsyClient(creds)
    try:
        shop = graph.get_shop(creds.shop_id)
    except EtsyError as exc:
        checks["shop"] = f"blocked: {exc}"
        return report
    shop_id = str(shop.get("shop_id") or creds.shop_id)
    shop_name = str(shop.get("shop_name") or "")
    currency = str(shop.get("currency_code") or shop.get("currency") or "")
    checks["shop_id"] = "ok" if shop_id_matches(shop_id) else f"blocked: got {shop_id}"
    checks["shop_name"] = (
        "ok"
        if shop_name_matches(shop_name) or shop_id_matches(shop_id)
        else f"blocked: got {shop_name!r}"
    )
    try:
        assert_shop_currency(currency)
        checks["currency"] = f"ok ({REQUIRED_CURRENCY})"
    except EtsyError as exc:
        checks["currency"] = f"blocked: {exc}"
        return report
    scopes = (creds.scopes or "").replace(",", " ").split()
    required = {"listings_r", "listings_w", "listings_d", "shops_r", "transactions_r"}
    missing = sorted(required - set(scopes))
    checks["scopes"] = "ok" if not missing else f"blocked: missing {', '.join(missing)}"
    if pack_id:
        try:
            folder, manifest = load_pack(pack_id, packs=packs or packs_dir())
            assert_etsy_ready(manifest, folder)
            preview_image(folder)
            files = digital_files(folder)
            checks["pack"] = "ok"
            checks["preview"] = preview_image(folder).name
            checks["digital_file"] = files[0].name
            checks["quantity"] = 1
            checks["listing_type"] = "download"
        except EtsyError as exc:
            checks["pack"] = f"blocked: {exc}"
            return report
    blocked = [name for name, value in checks.items() if str(value).startswith("blocked")]
    report["ready_to_publish"] = not blocked
    report["shop_id"] = shop_id
    report["shop_name"] = shop_name
    report["currency"] = currency
    return report
