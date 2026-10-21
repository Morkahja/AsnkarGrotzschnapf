"""Minimal Etsy Open API v3 client for digital listings and sales."""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from anskar.etsy.credentials import EtsyCredentials
from anskar.etsy.errors import EtsyError, redact

API_HOST = "https://api.etsy.com/v3/application"
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes, dict[str, str]]]


class EtsyClient:
    def __init__(
        self,
        credentials: EtsyCredentials,
        *,
        transport: Transport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.credentials = credentials
        self.transport = transport or _default_transport
        self.timeout = timeout

    def create_draft_listing(self, fields: dict[str, object]) -> dict[str, object]:
        price = fields.get("price")
        if price is None:
            raise EtsyError("Listing price is required.", kind="credentials")
        body = {
            "quantity": int(fields.get("quantity") or 1),
            "title": fields["title"],
            "description": fields["description"],
            "price": float(price),
            "who_made": fields.get("who_made", "i_did"),
            "when_made": fields.get("when_made", "2020_2026"),
            "taxonomy_id": self.credentials.taxonomy_id,
            "type": "download",
            "should_auto_renew": bool(fields.get("should_auto_renew", False)),
            "is_supply": False,
            "tags": fields.get("tags") or [],
            "materials": fields.get("materials") or [],
        }
        if self.credentials.return_policy_id:
            body["return_policy_id"] = int(self.credentials.return_policy_id)
        return self._json(
            "POST",
            f"/shops/{self.credentials.shop_id}/listings",
            body=body,
        )

    def upload_listing_image(
        self,
        listing_id: str,
        path: Path,
        *,
        rank: int | None = None,
    ) -> dict[str, object]:
        fields: dict[str, str] = {}
        if rank is not None:
            fields["rank"] = str(int(rank))
        return self._multipart(
            "POST",
            f"/shops/{self.credentials.shop_id}/listings/{listing_id}/images",
            fields=fields,
            file_field="image",
            file_path=path,
        )

    def delete_listing_image(self, listing_id: str, listing_image_id: str | int) -> dict[str, object]:
        return self._json(
            "DELETE",
            f"/shops/{self.credentials.shop_id}/listings/{listing_id}/images/{listing_image_id}",
            body=None,
        )

    def upload_listing_file(self, listing_id: str, path: Path) -> dict[str, object]:
        return self._multipart(
            "POST",
            f"/shops/{self.credentials.shop_id}/listings/{listing_id}/files",
            fields={"name": path.name},
            file_field="file",
            file_path=path,
        )

    def publish_listing(self, listing_id: str) -> dict[str, object]:
        return self._json(
            "PATCH",
            f"/shops/{self.credentials.shop_id}/listings/{listing_id}",
            body={"state": "active"},
        )

    def update_listing(self, listing_id: str, fields: dict[str, object]) -> dict[str, object]:
        return self._json(
            "PATCH",
            f"/shops/{self.credentials.shop_id}/listings/{listing_id}",
            body=fields,
        )

    def get_listing_inventory(self, listing_id: str) -> dict[str, object]:
        return self._json("GET", f"/listings/{listing_id}/inventory", body=None)

    def update_listing_inventory(
        self,
        listing_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        return self._json("PUT", f"/listings/{listing_id}/inventory", body=body)

    def update_listing_price(self, listing_id: str, price: float) -> dict[str, object]:
        """
        Active digital listings ignore price on PATCH; inventory PUT is required.
        """
        inventory = self.get_listing_inventory(listing_id)
        products: list[dict[str, object]] = []
        for product in results_list(inventory, "products"):
            if product.get("is_deleted"):
                continue
            offerings: list[dict[str, object]] = []
            for offering in results_list(product, "offerings"):
                if offering.get("is_deleted"):
                    continue
                offerings.append(
                    {
                        "price": float(price),
                        "quantity": int(offering.get("quantity") or 1),
                        "is_enabled": bool(offering.get("is_enabled", True)),
                    }
                )
            if not offerings:
                continue
            products.append(
                {
                    "sku": product.get("sku") or "",
                    "property_values": product.get("property_values") or [],
                    "offerings": offerings,
                }
            )
        if not products:
            raise EtsyError("Listing has no inventory offerings to price.", kind="api")
        return self.update_listing_inventory(
            listing_id,
            {
                "products": products,
                "price_on_property": inventory.get("price_on_property") or [],
                "quantity_on_property": inventory.get("quantity_on_property") or [],
                "sku_on_property": inventory.get("sku_on_property") or [],
            },
        )

    def update_listing_property(
        self,
        listing_id: str,
        property_id: int,
        *,
        value_ids: list[int | None],
        values: list[str],
        scale_id: int | None = None,
    ) -> dict[str, object]:
        """
        Set one taxonomy attribute via form-urlencoded PUT.

        Continuous measures (width/height) need an empty value_ids entry so Etsy
        assigns the id; discrete attributes send the known value_id.
        """
        pairs: list[tuple[str, str]] = []
        for value_id in value_ids:
            pairs.append(("value_ids", "" if value_id is None else str(int(value_id))))
        for value in values:
            pairs.append(("values", value))
        if scale_id is not None:
            pairs.append(("scale_id", str(int(scale_id))))
        return self._form(
            "PUT",
            f"/shops/{self.credentials.shop_id}/listings/{listing_id}/properties/{property_id}",
            pairs,
        )

    def get_listing_properties(self, listing_id: str) -> dict[str, object]:
        return self._json(
            "GET",
            f"/shops/{self.credentials.shop_id}/listings/{listing_id}/properties",
            body=None,
        )

    def delete_listing(self, listing_id: str) -> dict[str, object]:
        return self._json("DELETE", f"/listings/{listing_id}", body=None)

    def get_listing(self, listing_id: str) -> dict[str, object]:
        return self._json("GET", f"/listings/{listing_id}", body=None)

    def get_listing_images(self, listing_id: str) -> dict[str, object]:
        # Shop-scoped GET returns 404 on some shops; listing-scoped path is reliable.
        return self._json(
            "GET",
            f"/listings/{listing_id}/images",
            body=None,
        )

    def get_listing_files(self, listing_id: str) -> dict[str, object]:
        try:
            return self._json(
                "GET",
                f"/shops/{self.credentials.shop_id}/listings/{listing_id}/files",
                body=None,
            )
        except EtsyError:
            return self._json(
                "GET",
                f"/listings/{listing_id}/files",
                body=None,
            )

    def get_shop(self, shop_id: str | None = None) -> dict[str, object]:
        target = shop_id or self.credentials.shop_id
        return self._json("GET", f"/shops/{target}", body=None)

    def get_user_shops(self, user_id: str) -> dict[str, object]:
        return self._json("GET", f"/users/{user_id}/shops", body=None)

    def get_shop_receipts(
        self,
        *,
        was_paid: bool | None = True,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, object]:
        query: dict[str, str] = {
            "limit": str(limit),
            "offset": str(offset),
        }
        if was_paid is not None:
            query["was_paid"] = "true" if was_paid else "false"
        encoded = urllib.parse.urlencode(query)
        return self._json(
            "GET",
            f"/shops/{self.credentials.shop_id}/receipts?{encoded}",
            body=None,
        )

    def get_shop_receipt_transactions(self, receipt_id: str) -> dict[str, object]:
        return self._json(
            "GET",
            f"/shops/{self.credentials.shop_id}/receipts/{receipt_id}/transactions",
            body=None,
        )

    def get_shop_transactions(self, *, limit: int = 100, offset: int = 0) -> dict[str, object]:
        query = urllib.parse.urlencode({"limit": str(limit), "offset": str(offset)})
        return self._json(
            "GET",
            f"/shops/{self.credentials.shop_id}/transactions?{query}",
            body=None,
        )

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.credentials.access_token}",
            "x-api-key": self.credentials.x_api_key(),
            "Accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _json(self, method: str, path: str, *, body: dict | None) -> dict[str, object]:
        payload = None
        headers = self._headers(content_type="application/json" if body is not None else None)
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
        status, raw, _ = self.transport(method, API_HOST + path, headers, payload, self.timeout)
        return self._parse(status, raw)

    def _form(
        self,
        method: str,
        path: str,
        pairs: list[tuple[str, str]],
    ) -> dict[str, object]:
        payload = urllib.parse.urlencode(pairs, doseq=True).encode("utf-8")
        headers = self._headers(content_type="application/x-www-form-urlencoded")
        status, raw, _ = self.transport(method, API_HOST + path, headers, payload, self.timeout)
        return self._parse(status, raw)

    def _multipart(
        self,
        method: str,
        path: str,
        *,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
    ) -> dict[str, object]:
        boundary = "----AnskarEtsyBoundary7MA4YWxkTrZu0gW"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(f"--{boundary}\r\n".encode())
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            chunks.append(value.encode("utf-8"))
            chunks.append(b"\r\n")
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        data = file_path.read_bytes()
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode()
        )
        chunks.append(data)
        chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        body = b"".join(chunks)
        headers = self._headers(content_type=f"multipart/form-data; boundary={boundary}")
        status, raw, _ = self.transport(method, API_HOST + path, headers, body, self.timeout)
        return self._parse(status, raw)

    def _parse(self, status: int, raw: bytes) -> dict[str, object]:
        text = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise EtsyError(
                redact(f"Etsy returned non-JSON (HTTP {status}).", self.credentials.access_token),
                kind="api",
            ) from exc
        if status >= 400:
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("error") or payload.get("error_msg") or payload)
            raise EtsyError(
                redact(f"Etsy API error HTTP {status}: {message}", self.credentials.access_token),
                kind="api",
            )
        if payload == {} and status in {204, 200}:
            return {}
        if not isinstance(payload, dict):
            raise EtsyError("Etsy returned an unexpected payload.", kind="api")
        return payload


def listing_url(listing_id: str, shop_name: str | None = None) -> str:
    return f"https://www.etsy.com/listing/{listing_id}"


def results_list(payload: dict[str, object], *keys: str) -> list[dict]:
    for key in keys or ("results",):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"", dict(exc.headers.items()) if exc.headers else {}
    except TimeoutError as exc:
        raise EtsyError("The Etsy request timed out.", kind="timeout") from exc
    except OSError as exc:
        raise EtsyError("The Etsy request could not connect.", kind="api") from exc
