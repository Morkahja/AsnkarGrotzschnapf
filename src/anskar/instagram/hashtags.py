"""Instagram hashtags for Anskar captions.

Five tags, in this order:
1. literary inspiration from the poem (Ollama when available)
2. artistic style of the image (Ollama when available, else from recorded direction)
3. #AIgeneratedart
4. #DigitalArtForSale
5. #AnskarGrotzschnapf
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Callable

from anskar.interpret import MODEL as OLLAMA_MODEL

ARTIST_HASHTAG = "#AnskarGrotzschnapf"
AI_HASHTAG = "#AIgeneratedart"
SALE_HASHTAG = "#DigitalArtForSale"

STATIC_HASHTAGS = (AI_HASHTAG, SALE_HASHTAG, ARTIST_HASHTAG)

_OLLAMA_HOST = os.environ.get("ANSKAR_OLLAMA_HOST", "http://127.0.0.1:11434")
_CHAT_TIMEOUT = 45.0

_HASHTAG_INSTRUCTION = """You choose two Instagram hashtags for Anskar Grotzschnapf artwork.
Reply with JSON only, no markdown:
{"literary":"...","style":"..."}
literary: one short theme inspired by the poem (two to four words, no # symbol).
style: one short artistic-style label for the image (two to four words, no # symbol).
Examples: {"literary":"eternal love","style":"surreal abstraction"}
Do not invent brand names. Do not quote the poem.
"""


Chooser = Callable[..., list[str]]


def to_hashtag(label: str) -> str:
    """Turn a short phrase into one Instagram hashtag body with a leading #."""
    words = re.findall(r"[A-Za-z0-9]+", label or "")
    if not words:
        return ""
    body = "".join(word[:1].upper() + word[1:] for word in words)
    if not body:
        return ""
    return f"#{body}"


def fallback_literary_hashtag(*, title: str | None, author: str | None = None) -> str:
    del author  # reserved for richer fallbacks later
    tag = to_hashtag(str(title or "").strip())
    if tag and len(tag) > 2:
        return tag
    return "#LiteraryArt"


def fallback_style_hashtag(
    *,
    abstraction: str | None = None,
    style_preset: str | None = None,
) -> str:
    level = (abstraction or "").strip().lower()
    preset = (style_preset or "").strip().lower()
    if level == "experimental":
        base = "ExperimentalArt"
    elif level == "gentle":
        base = "GentleSurrealism"
    else:
        base = "SurrealArt"
    if preset in {"painterly", "dreamlike", "symbolic"} and preset != "none":
        return to_hashtag(f"{preset} {base}")
    return f"#{base}"


def build_hashtags(
    *,
    literary: str,
    style: str,
) -> list[str]:
    """Assemble the five-tag block. Drops empties and duplicates while keeping order."""
    ordered = [literary, style, *STATIC_HASHTAGS]
    seen: set[str] = set()
    result: list[str] = []
    for tag in ordered:
        cleaned = tag.strip()
        if not cleaned.startswith("#") or len(cleaned) < 2:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def choose_hashtags(
    *,
    title: str | None = None,
    author: str | None = None,
    poem: str | None = None,
    abstraction: str | None = None,
    style_preset: str | None = None,
    visual_prompt: str | None = None,
    transport=None,
    allow_ollama: bool = True,
) -> list[str]:
    """
    Return the five Instagram hashtags.

    Tries Ollama for literary and style labels. On any failure, uses local fallbacks
    so packing and publishing never block on the language model.
    """
    literary = ""
    style = ""
    if allow_ollama:
        try:
            if transport is None and not _ollama_reachable():
                raise OSError("Ollama is not reachable")
            literary, style = _ollama_labels(
                title=title,
                author=author,
                poem=poem,
                abstraction=abstraction,
                style_preset=style_preset,
                visual_prompt=visual_prompt,
                transport=transport,
            )
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError, KeyError):
            literary, style = "", ""
    literary_tag = to_hashtag(literary) if literary else fallback_literary_hashtag(title=title, author=author)
    style_tag = to_hashtag(style) if style else fallback_style_hashtag(
        abstraction=abstraction,
        style_preset=style_preset,
    )
    if not literary_tag:
        literary_tag = fallback_literary_hashtag(title=title, author=author)
    if not style_tag:
        style_tag = fallback_style_hashtag(abstraction=abstraction, style_preset=style_preset)
    return build_hashtags(literary=literary_tag, style=style_tag)


def format_hashtag_block(tags: list[str]) -> str:
    if not tags:
        return ""
    return "\n\n" + " ".join(tags)


def append_hashtags(caption: str, tags: list[str]) -> str:
    block = format_hashtag_block(tags)
    if not block:
        return caption
    if caption.rstrip().endswith(block.strip()):
        return caption
    return caption.rstrip() + block


def hashtags_from_manifest(manifest: dict) -> list[str]:
    stored = manifest.get("instagram_hashtags")
    if isinstance(stored, list) and all(isinstance(item, str) for item in stored):
        cleaned = [item.strip() for item in stored if item and item.strip().startswith("#")]
        if cleaned:
            return cleaned
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
    return choose_hashtags(
        title=rights.get("title") if isinstance(rights.get("title"), str) else manifest.get("title"),
        author=rights.get("author") if isinstance(rights.get("author"), str) else manifest.get("author"),
        poem=manifest.get("source_text") if isinstance(manifest.get("source_text"), str) else None,
        abstraction=manifest.get("abstraction") if isinstance(manifest.get("abstraction"), str) else None,
        style_preset=manifest.get("style_preset") if isinstance(manifest.get("style_preset"), str) else None,
        visual_prompt=manifest.get("visual_prompt") if isinstance(manifest.get("visual_prompt"), str) else None,
        allow_ollama=False,
    )


def _ollama_labels(
    *,
    title: str | None,
    author: str | None,
    poem: str | None,
    abstraction: str | None,
    style_preset: str | None,
    visual_prompt: str | None,
    transport,
) -> tuple[str, str]:
    host = _OLLAMA_HOST.rstrip("/")
    user_parts = [
        f"Poem title: {title or 'unknown'}",
        f"Author: {author or 'unknown'}",
        f"Abstraction: {abstraction or 'surreal'}",
        f"Style preset: {style_preset or 'none'}",
    ]
    if poem and poem.strip():
        # Keep the request small; the full poem is not required for a theme tag.
        excerpt = poem.strip()
        if len(excerpt) > 1200:
            excerpt = excerpt[:1200].rstrip() + "…"
        user_parts.append(f"Poem text:\n{excerpt}")
    if visual_prompt and visual_prompt.strip():
        prompt = visual_prompt.strip()
        if len(prompt) > 600:
            prompt = prompt[:600].rstrip() + "…"
        user_parts.append(f"Visual prompt:\n{prompt}")
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "keep_alive": "5m",
        "options": {"temperature": 0.4, "num_predict": 80},
        "messages": [
            {"role": "system", "content": _HASHTAG_INSTRUCTION},
            {"role": "user", "content": "\n".join(user_parts)},
        ],
    }
    send = transport or _http_json
    response = send("POST", f"{host}/api/chat", payload, _CHAT_TIMEOUT)
    content = response.get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty hashtag response")
    data = _parse_json_object(content)
    literary = str(data.get("literary") or "").strip()
    style = str(data.get("style") or "").strip()
    if not literary or not style:
        raise ValueError("incomplete hashtag response")
    return literary, style


def _parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        loaded = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        loaded = json.loads(match.group(0))
    if not isinstance(loaded, dict):
        raise ValueError("hashtag response is not an object")
    return loaded


def _ollama_reachable() -> bool:
    host = _OLLAMA_HOST.rstrip("/")
    try:
        request = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(request, timeout=2) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _http_json(method: str, url: str, body: dict | None, timeout: float) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Ollama returned a non-object response")
    return payload
