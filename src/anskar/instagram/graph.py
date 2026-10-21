"""Instagram API with Instagram Login. Calls stay on graph.instagram.com."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from anskar.instagram.errors import InstagramError, redact

GRAPH_HOST = "https://graph.instagram.com"
PROFILE_FIELDS = "user_id,username,account_type"
CONTAINER_FIELDS = "id,status_code,status"
MEDIA_FIELDS = "id,permalink,caption"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]


Transport = Callable[[str, str, dict[str, str], float], HttpResponse]


class GraphClient:
    """Small client. The access token is sent and then removed from errors."""

    def __init__(
        self,
        token: str,
        *,
        version: str = "v22.0",
        transport: Transport | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not token.strip():
            raise InstagramError("No Instagram token is available.", kind="credentials")
        self._token = token.strip()
        self.version = version
        self._transport = transport or _urllib_transport
        self.timeout = timeout

    def profile(self) -> dict[str, object]:
        return self._json(
            "GET",
            f"/{self.version}/me",
            {"fields": PROFILE_FIELDS},
        )

    def refresh(self) -> dict[str, object]:
        """Meta's long-lived token refresh. It does not need the app secret."""
        return self._json(
            "GET",
            "/refresh_access_token",
            {"grant_type": "ig_refresh_token"},
        )

    def create_image(self, user_id: str, *, image_url: str, caption: str, alt_text: str) -> str:
        payload = self._json(
            "POST",
            f"/{self.version}/{user_id}/media",
            {"image_url": image_url, "caption": caption, "alt_text": alt_text},
        )
        return _required_id(payload, "Instagram did not return a media container.")

    def container_status(self, container_id: str) -> dict[str, object]:
        return self._json(
            "GET",
            f"/{self.version}/{container_id}",
            {"fields": CONTAINER_FIELDS},
        )

    def publish(self, user_id: str, creation_id: str) -> str:
        payload = self._json(
            "POST",
            f"/{self.version}/{user_id}/media_publish",
            {"creation_id": creation_id},
        )
        return _required_id(payload, "Instagram did not return a media id.")

    def media(self, media_id: str) -> dict[str, object]:
        return self._json(
            "GET",
            f"/{self.version}/{media_id}",
            {"fields": MEDIA_FIELDS},
        )

    def recent_media(self, user_id: str) -> list[dict[str, object]]:
        payload = self._json(
            "GET",
            f"/{self.version}/{user_id}/media",
            {"fields": MEDIA_FIELDS, "limit": "10"},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _json(self, method: str, path: str, params: dict[str, str]) -> dict[str, object]:
        if not path.startswith("/"):
            raise InstagramError("The Instagram request path is not valid.", kind="api")
        url = GRAPH_HOST + path
        query = dict(params)
        query["access_token"] = self._token
        try:
            response = self._transport(method, url, query, self.timeout)
        except TimeoutError as exc:
            raise InstagramError("The Instagram request timed out.", kind="timeout") from exc
        except InstagramError:
            raise
        except OSError as exc:
            raise InstagramError("The Instagram request could not connect.", kind="network") from exc
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstagramError(
                f"Instagram returned an unreadable response (HTTP {response.status}).",
                kind="api",
            ) from exc
        if not isinstance(payload, dict):
            raise InstagramError("Instagram returned an unexpected response.", kind="api")
        if response.status >= 400 or "error" in payload:
            raise _api_error(response.status, payload, self._token)
        return payload


def _required_id(payload: dict[str, object], message: str) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value.strip():
        raise InstagramError(message, kind="api")
    return value.strip()


def _api_error(status: int, payload: dict[str, object], token: str) -> InstagramError:
    error = payload.get("error")
    message = ""
    code = None
    subcode = None
    trace = ""
    if isinstance(error, dict):
        message = redact(str(error.get("message") or ""), token)
        code = _int(error.get("code"))
        subcode = _int(error.get("error_subcode"))
        trace = str(error.get("fbtrace_id") or "")
    lowered = message.lower()
    kind = "api"
    summary = "Instagram refused the request."
    if status == 429 or code in {4, 17, 32, 613} or "request limit" in lowered:
        kind = "rate_limit"
        summary = "Instagram rate-limited the request."
    elif code == 190 and subcode == 463:
        kind = "expired"
        summary = (
            "The Instagram token is expired. Generate a new token in the Meta developer "
            "dashboard and install it with `anskar instagram install-token --from FILE`. "
            "Do not paste the token into the chat."
        )
    elif code == 190 or status == 401:
        kind = "invalid_token"
        summary = (
            "The Instagram token was rejected. Install a current token with "
            "`anskar instagram install-token --from FILE`. Do not paste the token into the chat."
        )
    elif "api access blocked" in lowered:
        kind = "access_blocked"
        summary = "Meta blocked API access for this app or account."
    elif code in {10, 200, 294} or "permission" in lowered:
        kind = "permission"
        summary = (
            "The token is missing Instagram permission. Publishing needs "
            "instagram_business_basic and instagram_business_content_publish."
        )
    detail = summary
    if message and kind not in {"expired", "invalid_token"}:
        detail = f"{summary} Meta said: {message}"
    if code is not None:
        detail = f"{detail} (code {code}"
        detail += f", subcode {subcode})" if subcode is not None else ")"
    if trace:
        detail = f"{detail} fbtrace_id={trace}"
    return InstagramError(detail, kind=kind)


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _urllib_transport(method: str, url: str, params: dict[str, str], timeout: float) -> HttpResponse:
    if urllib.parse.urlparse(url).scheme != "https" or urllib.parse.urlparse(url).hostname != "graph.instagram.com":
        raise InstagramError("Instagram requests stay on graph.instagram.com.", kind="api")
    target = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(target, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read(), dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {})
    except (TimeoutError, socket.timeout) as exc:
        raise TimeoutError("timed out") from exc
