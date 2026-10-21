"""Etsy errors. Messages never include tokens or secrets."""

from __future__ import annotations

import re

_TOKENISH = re.compile(
    r"(access_token|refresh_token|shared_secret|api_key|x-api-key|code_verifier|client_secret)"
    r"[=:]\s*\S+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_CODE = re.compile(r"(?:^|[?&])code=[^&\s]+", re.IGNORECASE)


class EtsyError(Exception):
    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(redact(message))
        self.kind = kind


def redact(text: str, secret: str | None = None) -> str:
    cleaned = _TOKENISH.sub(r"\1=[redacted]", text)
    cleaned = _BEARER.sub("Bearer [redacted]", cleaned)
    cleaned = _CODE.sub(
        lambda match: match.group(0).split("=", 1)[0] + "=[redacted]",
        cleaned,
    )
    if secret:
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned
