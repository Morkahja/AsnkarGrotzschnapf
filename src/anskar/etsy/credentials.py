"""Etsy OAuth credentials outside the repository."""

from __future__ import annotations

import getpass
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anskar.etsy.errors import EtsyError
from anskar.etsy.oauth import (
    DEFAULT_REDIRECT_URI,
    DEFAULT_SCOPES,
    EXPECTED_SHOP_NAME,
    TokenResponse,
    normalize_redirect_uri,
    refresh_access_token,
)

DEFAULT_TAXONOMY_ID = 2078  # Digital Prints (configurable)
REFRESH_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class EtsyCredentials:
    api_key: str
    shared_secret: str
    access_token: str
    shop_id: str
    taxonomy_id: int = DEFAULT_TAXONOMY_ID
    return_policy_id: str | None = None
    refresh_token: str | None = None
    expires_at: str | None = None
    scopes: str | None = None
    shop_name: str | None = None
    redirect_uri: str = DEFAULT_REDIRECT_URI
    price: str = "12.00"
    currency: str = "USD"
    quantity: int = 999
    path: Path | None = None

    def x_api_key(self) -> str:
        """Etsy requires keystring:shared_secret on every request."""
        return f"{self.api_key}:{self.shared_secret}"

    def public(self) -> dict[str, object]:
        return {
            "path": str(self.path) if self.path else None,
            "shop_id": self.shop_id,
            "shop_name": self.shop_name,
            "expected_shop_name": EXPECTED_SHOP_NAME,
            "taxonomy_id": self.taxonomy_id,
            "return_policy_id": self.return_policy_id,
            "price": self.price,
            "currency": self.currency,
            "quantity": self.quantity,
            "configured": True,
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "has_api_key": bool(self.api_key),
            "has_shared_secret": bool(self.shared_secret),
            "scopes": self.scopes,
            "expires_at": self.expires_at,
            "token_valid": self.token_valid(),
            "redirect_uri": self.redirect_uri,
        }

    def token_valid(self, *, now: datetime | None = None) -> bool:
        if not self.access_token or self.access_token == "pending-oauth":
            return False
        if not self.expires_at:
            return True
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        return clock + REFRESH_SKEW < expiry

    def oauth_complete(self) -> bool:
        return (
            bool(self.api_key)
            and bool(self.shared_secret)
            and bool(self.access_token)
            and self.access_token != "pending-oauth"
            and bool(self.shop_id)
            and self.shop_id not in {"pending", "0"}
        )


def etsy_credentials_path() -> Path:
    override = os.environ.get("ANSKAR_ETSY_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "anskar" / "etsy.json"


def etsy_tls_dir() -> Path:
    override = os.environ.get("ANSKAR_ETSY_TLS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "anskar" / "etsy-tls"


def etsy_configured(path: Path | None = None) -> bool:
    """True when OAuth has produced usable tokens for a real shop id."""
    file = path or etsy_credentials_path()
    if not file.is_file():
        return False
    try:
        return load_etsy_credentials(file).oauth_complete()
    except EtsyError:
        return False


def load_etsy_credentials(path: Path | None = None) -> EtsyCredentials:
    file = path or etsy_credentials_path()
    if not file.is_file():
        raise EtsyError(
            "No Etsy credentials are stored. Run `anskar etsy setup` for the API key, "
            "then `anskar etsy connect` to authorize the shop. "
            "Do not paste secrets into the chat.",
            kind="credentials",
        )
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EtsyError("The Etsy credential file could not be read.", kind="credentials") from exc
    if not isinstance(payload, dict):
        raise EtsyError("The Etsy credential file is not a record.", kind="credentials")
    return credentials_from_payload(payload, path=file)


def load_app_credentials(path: Path | None = None) -> tuple[str, str, str]:
    """Return (api_key, shared_secret, redirect_uri) for starting OAuth."""
    file = path or etsy_credentials_path()
    if file.is_file():
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EtsyError("The Etsy credential file could not be read.", kind="credentials") from exc
        if isinstance(payload, dict):
            api_key = payload.get("api_key")
            shared = payload.get("shared_secret")
            redirect = payload.get("redirect_uri")
            if isinstance(api_key, str) and api_key.strip() and isinstance(shared, str) and shared.strip():
                return (
                    api_key.strip(),
                    shared.strip(),
                    normalize_redirect_uri(redirect if isinstance(redirect, str) else None),
                )
    raise EtsyError(
        "Store the Etsy API keystring and shared secret first with `anskar etsy setup`, "
        "then run `anskar etsy connect`.",
        kind="credentials",
    )


def credentials_from_payload(payload: dict, *, path: Path | None = None) -> EtsyCredentials:
    required = ("api_key", "shared_secret", "access_token", "shop_id")
    values: dict[str, str] = {}
    for name in required:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise EtsyError(f"The Etsy credential file has no {name}.", kind="credentials")
        values[name] = value.strip()
    taxonomy = payload.get("taxonomy_id", DEFAULT_TAXONOMY_ID)
    try:
        taxonomy_id = int(taxonomy)
    except (TypeError, ValueError) as exc:
        raise EtsyError("The Etsy taxonomy_id must be an integer.", kind="credentials") from exc
    return_policy = payload.get("return_policy_id")
    refresh = payload.get("refresh_token")
    price = payload.get("price", "12.00")
    currency = payload.get("currency", "USD")
    quantity = payload.get("quantity", 999)
    try:
        quantity_int = int(quantity)
    except (TypeError, ValueError) as exc:
        raise EtsyError("The Etsy quantity must be an integer.", kind="credentials") from exc
    expires_at = payload.get("expires_at")
    scopes = payload.get("scopes")
    shop_name = payload.get("shop_name")
    redirect = payload.get("redirect_uri")
    return EtsyCredentials(
        api_key=values["api_key"],
        shared_secret=values["shared_secret"],
        access_token=values["access_token"],
        shop_id=values["shop_id"],
        taxonomy_id=taxonomy_id,
        return_policy_id=return_policy.strip()
        if isinstance(return_policy, str) and return_policy.strip()
        else None,
        refresh_token=refresh.strip() if isinstance(refresh, str) and refresh.strip() else None,
        expires_at=expires_at.strip() if isinstance(expires_at, str) and expires_at.strip() else None,
        scopes=scopes.strip() if isinstance(scopes, str) and scopes.strip() else None,
        shop_name=shop_name.strip() if isinstance(shop_name, str) and shop_name.strip() else None,
        redirect_uri=normalize_redirect_uri(redirect if isinstance(redirect, str) else None),
        price=str(price).strip() if price is not None else "12.00",
        currency=str(currency).strip() if currency is not None else "USD",
        quantity=quantity_int,
        path=path,
    )


def setup_etsy_interactive(
    *,
    destination: Path | None = None,
    input_fn=input,
    getpass_fn=getpass.getpass,
) -> EtsyCredentials:
    """Store API key material (and optional manual token). Prefer `etsy connect` for OAuth."""
    print("Etsy Open API setup for Anskar Gortzschnapf Publisher → Anskartprints.")
    print("Enter the API keystring and shared secret from the Etsy developers page.")
    print("They are stored under ~/.config/anskar/ with mode 0600. Do not paste them into chat.")
    api_key = input_fn("Etsy API key (keystring): ").strip()
    shared = getpass_fn("Etsy shared secret: ").strip()
    redirect = normalize_redirect_uri(
        input_fn(f"Registered HTTPS callback URL [{DEFAULT_REDIRECT_URI}]: ").strip()
        or DEFAULT_REDIRECT_URI
    )
    if not redirect.startswith("https://"):
        raise EtsyError("The callback URL must be an https:// URL registered with Etsy.", kind="credentials")
    token = getpass_fn("Etsy access token (leave blank to use `etsy connect`): ").strip()
    shop_id = input_fn("Etsy shop id (leave blank until connect): ").strip()
    taxonomy = input_fn(f"Taxonomy id [{DEFAULT_TAXONOMY_ID}]: ").strip() or str(DEFAULT_TAXONOMY_ID)
    return_policy = input_fn("Return policy id (optional): ").strip() or None
    price = input_fn("Default price [12.00]: ").strip() or "12.00"
    if not api_key or not shared:
        raise EtsyError("API key and shared secret are required.", kind="credentials")
    try:
        taxonomy_id = int(taxonomy)
    except ValueError as exc:
        raise EtsyError("Taxonomy id must be an integer.", kind="credentials") from exc
    # Allow app-only setup for OAuth connect (placeholder shop/token until connect).
    stored = EtsyCredentials(
        api_key=api_key,
        shared_secret=shared,
        access_token=token or "pending-oauth",
        shop_id=shop_id or "pending",
        taxonomy_id=taxonomy_id,
        return_policy_id=return_policy,
        price=price,
        redirect_uri=redirect,
        path=destination or etsy_credentials_path(),
    )
    _write_credentials(stored)
    return stored


def save_oauth_tokens(
    *,
    api_key: str,
    shared_secret: str,
    tokens: TokenResponse,
    shop_id: str,
    shop_name: str | None,
    redirect_uri: str,
    path: Path | None = None,
    existing: EtsyCredentials | None = None,
) -> EtsyCredentials:
    base = existing
    stored = EtsyCredentials(
        api_key=api_key,
        shared_secret=shared_secret,
        access_token=tokens.access_token,
        shop_id=shop_id,
        taxonomy_id=base.taxonomy_id if base else DEFAULT_TAXONOMY_ID,
        return_policy_id=base.return_policy_id if base else None,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at.isoformat(),
        scopes=tokens.scope or DEFAULT_SCOPES,
        shop_name=shop_name,
        redirect_uri=redirect_uri,
        price=base.price if base else "12.00",
        currency=base.currency if base else "USD",
        quantity=base.quantity if base else 999,
        path=path or (base.path if base else etsy_credentials_path()),
    )
    _write_credentials(stored)
    return stored


def ensure_fresh_credentials(
    credentials: EtsyCredentials | None = None,
    *,
    path: Path | None = None,
    now: datetime | None = None,
    refresher=None,
) -> EtsyCredentials:
    """Refresh the access token when expired or near expiry. Stores the new refresh token."""
    stored = credentials or load_etsy_credentials(path)
    if not stored.oauth_complete():
        raise EtsyError(
            "Etsy OAuth is not complete. Run `anskar etsy setup` then `anskar etsy connect`.",
            kind="credentials",
        )
    if stored.token_valid(now=now):
        return stored
    if not stored.refresh_token:
        raise EtsyError(
            "The Etsy access token has expired and no refresh token is stored. "
            "Run `anskar etsy connect` again.",
            kind="credentials",
        )
    refresh = refresher or refresh_access_token
    tokens = refresh(client_id=stored.api_key, refresh_token=stored.refresh_token, now=now)
    updated = replace(
        stored,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at.isoformat(),
        scopes=tokens.scope or stored.scopes,
    )
    _write_credentials(updated)
    return updated


def shop_name_matches(actual: str | None, expected: str = EXPECTED_SHOP_NAME) -> bool:
    if not actual:
        return False
    return actual.strip().casefold() == expected.strip().casefold()


def _write_credentials(credentials: EtsyCredentials) -> None:
    file = credentials.path or etsy_credentials_path()
    payload = {
        "provider": "etsy-open-api-v3",
        "api_key": credentials.api_key,
        "shared_secret": credentials.shared_secret,
        "access_token": credentials.access_token,
        "refresh_token": credentials.refresh_token,
        "expires_at": credentials.expires_at,
        "scopes": credentials.scopes,
        "shop_id": credentials.shop_id,
        "shop_name": credentials.shop_name,
        "redirect_uri": credentials.redirect_uri,
        "taxonomy_id": credentials.taxonomy_id,
        "return_policy_id": credentials.return_policy_id,
        "price": credentials.price,
        "currency": credentials.currency,
        "quantity": credentials.quantity,
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(file.parent, 0o700)
    except OSError:
        pass
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(file)
    try:
        os.chmod(file, 0o600)
    except OSError:
        pass
