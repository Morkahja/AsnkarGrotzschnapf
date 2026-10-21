"""One-time Etsy OAuth connect and read-only shop verification."""

from __future__ import annotations

import webbrowser
from typing import Callable

from anskar.etsy.client import EtsyClient
from anskar.etsy.credentials import (
    EtsyCredentials,
    ensure_fresh_credentials,
    etsy_credentials_path,
    etsy_tls_dir,
    load_app_credentials,
    load_etsy_credentials,
    save_oauth_tokens,
    shop_id_matches,
    shop_identity_matches,
    shop_name_matches,
)
from anskar.etsy.errors import EtsyError
from anskar.etsy.oauth import (
    DEFAULT_REDIRECT_URI,
    DEFAULT_SCOPES,
    EXPECTED_SHOP_ID,
    EXPECTED_SHOP_NAME,
    begin_auth_request,
    ensure_localhost_tls,
    exchange_authorization_code,
    normalize_redirect_uri,
    wait_for_authorization_code,
)
from anskar.etsy.state import load_etsy_state, public_etsy_state


def connect_etsy(
    *,
    open_browser: bool = True,
    confirm_shop: bool = False,
    shop_id_override: str | None = None,
    path=None,
    browser_opener: Callable[[str], object] | None = None,
) -> dict[str, object]:
    """
    Run the authorization-code + PKCE flow against the registered HTTPS callback.

    Does not change seller verification state (pending_verification stays as-is).
    """
    api_key, shared_secret, redirect_uri = load_app_credentials(path)
    redirect_uri = normalize_redirect_uri(redirect_uri)
    auth = begin_auth_request(
        client_id=api_key,
        redirect_uri=redirect_uri,
        scopes=DEFAULT_SCOPES,
    )
    cert, key = ensure_localhost_tls(etsy_tls_dir())
    opener = browser_opener or webbrowser.open
    if open_browser:
        opener(auth.authorize_url)
    # Guidance only — never includes tokens, codes, or the PKCE verifier.
    print("Open this URL if the browser did not open:")
    print(auth.authorize_url)
    print(f"Waiting for Etsy callback at {auth.redirect_uri} …")
    print(
        f"Approve access for Anskar Gortzschnapf Publisher on the {EXPECTED_SHOP_NAME} shop "
        f"(id {EXPECTED_SHOP_ID})."
    )
    print("If the browser warns about the local HTTPS certificate, accept it for localhost only.")
    code = wait_for_authorization_code(
        redirect_uri=auth.redirect_uri,
        expected_state=auth.state,
        cert_file=cert,
        key_file=key,
    )
    tokens = exchange_authorization_code(
        client_id=api_key,
        code=code,
        code_verifier=auth.pkce.verifier,
        redirect_uri=auth.redirect_uri,
    )
    # Temporary credentials to call shop endpoints.
    provisional = EtsyCredentials(
        api_key=api_key,
        shared_secret=shared_secret,
        access_token=tokens.access_token,
        shop_id=shop_id_override or "0",
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at.isoformat(),
        scopes=tokens.scope,
        redirect_uri=auth.redirect_uri,
        path=path or etsy_credentials_path(),
    )
    client = EtsyClient(provisional)
    shop_id, shop_name = _resolve_shop(
        client,
        tokens_user_id=tokens.user_id,
        shop_id_override=shop_id_override,
        confirm_shop=confirm_shop,
        existing_shop_id=_existing_shop_id(path),
    )
    existing = None
    try:
        existing = load_etsy_credentials(path)
    except EtsyError:
        existing = None
    if (
        existing is not None
        and existing.shop_id not in {"", "pending", "0"}
        and existing.shop_id != shop_id
        and not confirm_shop
    ):
        raise EtsyError(
            f"Authenticated shop id {shop_id} differs from the stored shop id {existing.shop_id}. "
            "Re-run with --confirm-shop to replace it deliberately.",
            kind="credentials",
        )
    if not shop_identity_matches(shop_id=shop_id, shop_name=shop_name) and not confirm_shop:
        raise EtsyError(
            f"Authenticated shop id {shop_id} ({shop_name!r}) is not the confirmed shop "
            f"{EXPECTED_SHOP_NAME} (id {EXPECTED_SHOP_ID}). "
            "Pass --confirm-shop only if this is intentional.",
            kind="credentials",
        )
    stored = save_oauth_tokens(

        api_key=api_key,
        shared_secret=shared_secret,
        tokens=tokens,
        shop_id=shop_id,
        shop_name=shop_name,
        redirect_uri=auth.redirect_uri,
        path=path,
        existing=existing,
    )
    integration = public_etsy_state()
    return {
        "connected": True,
        "shop_id": stored.shop_id,
        "shop_name": stored.shop_name,
        "scopes": stored.scopes,
        "expires_at": stored.expires_at,
        "redirect_uri": stored.redirect_uri,
        "integration_state": integration.get("state"),
        "integration_unchanged": True,
        "path": str(stored.path) if stored.path else None,
    }


def etsy_connection_status(*, path=None) -> dict[str, object]:
    integration = public_etsy_state()
    report: dict[str, object] = {
        "integration_state": integration.get("state"),
        "oauth_configured": False,
        "shop_id": None,
        "shop_name": None,
        "scopes": None,
        "token_valid": False,
        "expires_at": None,
        "redirect_uri": DEFAULT_REDIRECT_URI,
        "expected_shop_id": EXPECTED_SHOP_ID,
        "expected_shop_name": EXPECTED_SHOP_NAME,
    }
    try:
        creds = load_etsy_credentials(path)
    except EtsyError as exc:
        if exc.kind == "credentials":
            report["detail"] = str(exc)
            return report
        raise
    if creds.access_token in {"", "pending-oauth"} or creds.shop_id in {"", "pending"}:
        report["detail"] = "API key is stored, but OAuth connect has not completed."
        report["has_api_key"] = True
        report["redirect_uri"] = creds.redirect_uri
        return report
    public = creds.public()
    report.update(
        {
            "oauth_configured": True,
            "shop_id": public["shop_id"],
            "shop_name": public["shop_name"],
            "scopes": public["scopes"],
            "token_valid": public["token_valid"],
            "expires_at": public["expires_at"],
            "redirect_uri": public["redirect_uri"],
            "has_refresh_token": public["has_refresh_token"],
            "shop_id_matches": shop_id_matches(public["shop_id"]),
            "name_matches": shop_name_matches(
                public["shop_name"] if isinstance(public["shop_name"], str) else None
            ),
        }
    )
    return report


def test_etsy_connection(*, path=None) -> dict[str, object]:
    """Read-only authenticated shop check. Never creates or deletes listings."""
    creds = ensure_fresh_credentials(path=path)
    client = EtsyClient(creds)
    shop = client.get_shop(creds.shop_id)
    shop_id = str(shop.get("shop_id") or creds.shop_id)
    shop_name = str(shop.get("shop_name") or "")
    id_matches_stored = str(shop_id) == str(creds.shop_id)
    id_matches_expected = shop_id_matches(shop_id)
    name_matches = shop_name_matches(shop_name)
    # Numeric shop id is authoritative; display-name drift must not fail the same shop.
    ok = id_matches_stored and shop_identity_matches(
        shop_id=shop_id,
        shop_name=shop_name,
        stored_shop_id=creds.shop_id,
    )
    return {
        "ok": ok,
        "shop_id": shop_id,
        "shop_name": shop_name,
        "expected_shop_id": EXPECTED_SHOP_ID,
        "expected_shop_name": EXPECTED_SHOP_NAME,
        "shop_id_matches": id_matches_expected,
        "name_matches": name_matches,
        "scopes": creds.scopes,
        "token_valid": creds.token_valid(),
        "integration_state": load_etsy_state(),
    }


def _existing_shop_id(path) -> str | None:
    try:
        existing = load_etsy_credentials(path)
    except EtsyError:
        return None
    if existing.shop_id in {"", "pending", "0"}:
        return None
    return existing.shop_id


def _resolve_shop(
    client: EtsyClient,
    *,
    tokens_user_id: str | None,
    shop_id_override: str | None,
    confirm_shop: bool,
    existing_shop_id: str | None,
) -> tuple[str, str]:
    if shop_id_override:
        shop = client.get_shop(shop_id_override)
        return str(shop.get("shop_id") or shop_id_override), str(shop.get("shop_name") or "")
    if tokens_user_id:
        payload = client.get_user_shops(tokens_user_id)
        shops = payload.get("results") if isinstance(payload.get("results"), list) else None
        if shops is None and isinstance(payload.get("shops"), list):
            shops = payload["shops"]
        if not isinstance(shops, list) or not shops:
            # Some responses return a single shop object.
            if payload.get("shop_id"):
                return str(payload["shop_id"]), str(payload.get("shop_name") or "")
            raise EtsyError("Etsy returned no shops for this account.", kind="api")
        preferred = None
        for item in shops:
            if not isinstance(item, dict):
                continue
            if shop_id_matches(item.get("shop_id")):
                preferred = item
                break
        if preferred is None:
            for item in shops:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("shop_name") or "")
                if shop_name_matches(name):
                    preferred = item
                    break
        if preferred is None:
            if len(shops) == 1 and isinstance(shops[0], dict) and confirm_shop:
                preferred = shops[0]
            elif existing_shop_id:
                return existing_shop_id, EXPECTED_SHOP_NAME
            else:
                names = [
                    str(item.get("shop_name") or item.get("shop_id"))
                    for item in shops
                    if isinstance(item, dict)
                ]
                raise EtsyError(
                    f"Could not find shop {EXPECTED_SHOP_NAME!r} (id {EXPECTED_SHOP_ID}) among {names}. "
                    "Pass --shop-id and --confirm-shop if needed.",
                    kind="credentials",
                )
        return str(preferred.get("shop_id")), str(preferred.get("shop_name") or "")
    if existing_shop_id:
        shop = client.get_shop(existing_shop_id)
        return str(shop.get("shop_id") or existing_shop_id), str(shop.get("shop_name") or "")
    raise EtsyError(
        "Could not determine the Etsy user id from the access token. Pass --shop-id.",
        kind="credentials",
    )
