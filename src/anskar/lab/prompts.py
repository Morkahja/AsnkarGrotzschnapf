"""Build an experiment prompt without rewriting the typed text."""

import re

MODES = ("title", "body", "title_body")


def compose_prompt(mode: str, title: str, body: str) -> str:
    if mode not in MODES:
        raise ValueError("Mode must be title, body, or title_body.")
    if mode == "title":
        if title.strip() == "":
            raise ValueError("Title is empty.")
        return title
    if mode == "body":
        if body.strip() == "":
            raise ValueError("Text is empty.")
        return body
    if title.strip() == "" or body.strip() == "":
        raise ValueError("Title and text are both required for this mode.")
    return f"{title}\n\n{body}"


def first_stanza(text: str) -> str:
    """Return the opening stanza, keeping its line breaks."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    stanza: list[str] = []
    for line in normalized.split("\n"):
        if line.strip() == "":
            if stanza:
                break
            continue
        stanza.append(line)
    return "\n".join(stanza)


def experiment_slug(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return cleaned[:80] or "ungrouped"
