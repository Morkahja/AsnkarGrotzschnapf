"""Public image URLs. No hosting service is started here."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from urllib.parse import urlparse

from anskar.instagram.credentials import hosts_path
from anskar.instagram.errors import InstagramError


def load_hosts(path: Path | None = None) -> dict[str, object]:
    file = path or hosts_path()
    if not file.is_file():
        return {"images": {}, "etsy_shop_url": None}
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstagramError("The image-host file could not be read.", kind="url") from exc
    if not isinstance(payload, dict):
        raise InstagramError("The image-host file is not a record.", kind="url")
    images = payload.get("images") or {}
    if not isinstance(images, dict):
        raise InstagramError("The image-host file has no image map.", kind="url")
    shop = payload.get("etsy_shop_url")
    return {
        "images": {str(key): value for key, value in images.items()},
        "etsy_shop_url": shop if isinstance(shop, str) and shop.strip() else None,
    }


def image_url(pack_id: str, hosts: dict[str, object] | None = None) -> str | None:
    record = hosts if hosts is not None else load_hosts()
    images = record.get("images")
    if not isinstance(images, dict):
        return None
    value = images.get(pack_id)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def etsy_shop_url(hosts: dict[str, object] | None = None) -> str | None:
    record = hosts if hosts is not None else load_hosts()
    value = record.get("etsy_shop_url")
    if not isinstance(value, str) or not value.strip():
        return None
    return validate_public_url(value.strip(), label="Etsy shop URL")


def validate_public_url(url: str, *, label: str = "Image URL") -> str:
    """Accept only an HTTPS URL Instagram's servers could fetch."""
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise InstagramError(f"{label} must be an https URL.", kind="url")
    if parsed.username or parsed.password:
        raise InstagramError(f"{label} must not contain a username or password.", kind="url")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise InstagramError(f"{label} must not point at this computer.", kind="url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise InstagramError(f"{label} must not point at a private address.", kind="url")
    return url.strip()
