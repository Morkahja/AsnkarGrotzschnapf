"""Errors for the Instagram connection. Messages never include a token."""

from __future__ import annotations

import re

_TOKEN_QUERY = re.compile(r"(access_token=)[^&\s\"']+", re.IGNORECASE)
_PRESIGNED = re.compile(r"https://[^\s\"']+\?[^\s\"']*X-Amz-[^\s\"']+", re.IGNORECASE)
_SECRETISH = re.compile(r"(secret[_-]?access[_-]?key|aws_secret)[=:]\s*\S+", re.IGNORECASE)


class InstagramError(Exception):
    """A local or Instagram failure that is safe to show."""

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(redact(message))
        self.kind = kind


def redact(text: str, secret: str | None = None) -> str:
    """Remove access tokens, secrets, and presigned URLs from a message."""
    cleaned = _TOKEN_QUERY.sub(r"\1[redacted]", text)
    cleaned = _PRESIGNED.sub("[redacted-url]", cleaned)
    cleaned = _SECRETISH.sub(r"\1=[redacted]", cleaned)
    if secret:
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned
