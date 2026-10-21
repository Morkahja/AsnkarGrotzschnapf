"""Minimal Etsy Open API v3 client for digital listings."""

from __future__ import annotations

import json
import mimetypes
import urllib.error
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
        body = {
            "quantity": self.credentials.quantity,
            "title": fields["title"],
            "description": fields["description"],
            "price": self.credentials.price,
            "who_made": fields.get("who_made", "i_did"),
            "when_made": fields.get("when_made", "2020_2025"),
            "taxonomy_id": self.credentials.taxonomy_id,
            "type": "download",
            "should_auto_renew": True,
            "is_supply": False,
            "tags": fields.get("tags") or [],
        }
        if self.credentials.return_policy_id:
            body["return_policy_id"] = int(self.credentials.return_policy_id)
        return self._json(
            "POST",
            f"/shops/{self.credentials.shop_id}/listings",
            body=body,
        )

    def upload_listing_image(self, listing_id: str, path: Path) -> dict[str, object]:
        return self._multipart(
            "POST",
            f"/shops/{self.credentials.shop_id}/listings/{listing_id}/images",
            fields={},
            file_field="image",
            file_path=path,
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

    def get_listing(self, listing_id: str) -> dict[str, object]:
        return self._json("GET", f"/listings/{listing_id}", body=None)

    def get_shop(self, shop_id: str | None = None) -> dict[str, object]:
        target = shop_id or self.credentials.shop_id
        return self._json("GET", f"/shops/{target}", body=None)

    def get_user_shops(self, user_id: str) -> dict[str, object]:
        return self._json("GET", f"/users/{user_id}/shops", body=None)

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
        if not isinstance(payload, dict):
            raise EtsyError("Etsy returned an unexpected payload.", kind="api")
        return payload


def listing_url(listing_id: str, shop_name: str | None = None) -> str:
    if shop_name:
        return f"https://www.etsy.com/listing/{listing_id}"
    return f"https://www.etsy.com/listing/{listing_id}"


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
