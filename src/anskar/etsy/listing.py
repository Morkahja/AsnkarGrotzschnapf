"""Build Etsy listing fields from a publish pack manifest."""

from __future__ import annotations

import json
from pathlib import Path

from anskar.etsy.errors import EtsyError
from anskar.etsy.attributes import DEFAULT_MATERIALS
from anskar.printmaker import DIGITAL_NOTICE

MAX_TITLE = 140
MAX_TAGS = 13
MAX_TAG_LEN = 20
MAX_DIGITAL_FILE_BYTES = 20 * 1024 * 1024  # Etsy per-file limit
SINGLE_SALE_NOTICE = (
    "Single-sale digital edition: quantity is one. "
    "When purchased, this listing is retired and is not restocked. "
    "Purchase grants personal download access for the buyer; "
    "it does not transfer copyright ownership or guarantee technical exclusivity."
)
AI_CUSTOMER_DISCLOSURE = (
    "This artwork was created using AI image generation software running locally."
)
POEM_INTRO = "The poem this image is based on:"


def load_pack(pack_id: str, *, packs: Path) -> tuple[Path, dict]:
    if (
        not pack_id
        or pack_id in {".", ".."}
        or "/" in pack_id
        or "\\" in pack_id
        or Path(pack_id).name != pack_id
    ):
        raise EtsyError("Pack id is not valid.", kind="media")
    folder = packs / pack_id
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        raise EtsyError(f"Publish pack not found: {pack_id}", kind="media")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EtsyError(f"Publish pack {pack_id} has no readable manifest.", kind="media") from exc
    if not isinstance(payload, dict):
        raise EtsyError(f"Publish pack {pack_id} has no readable manifest.", kind="media")
    return folder, payload


def assert_etsy_ready(manifest: dict, folder: Path) -> None:
    if not manifest.get("publication_ready"):
        raise EtsyError(
            "This pack is not publication-ready for Etsy. "
            "It needs documented poem rights, a print-suitable image, and a cleared model.",
            kind="media",
        )
    if not manifest.get("digital_download", True):
        raise EtsyError("This pack is not marked as a digital download.", kind="media")
    print_png = folder / "etsy" / "print.png"
    preview = folder / "etsy" / "preview.png"
    if not print_png.is_file():
        raise EtsyError("The Etsy print PNG is missing.", kind="media")
    if not preview.is_file():
        raise EtsyError("The Etsy preview PNG is missing.", kind="media")
    size = print_png.stat().st_size
    if size > MAX_DIGITAL_FILE_BYTES:
        raise EtsyError(
            f"The print file is {size} bytes; Etsy allows at most {MAX_DIGITAL_FILE_BYTES} bytes per file.",
            kind="media",
        )


def poem_text_cleared_for_reproduction(manifest: dict) -> bool:
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
    if rights.get("commercial_use_sweden") == "yes" and rights.get("publication_ready"):
        return True
    return bool(manifest.get("rights_cleared") and manifest.get("publication_ready"))


def listing_fields(manifest: dict, *, price_sek: float | None = None) -> dict[str, object]:
    """Customer-facing Etsy fields. price_sek is unused (kept for call-site compatibility)."""
    del price_sek  # Never mention price or VAT in the buyer description.
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
    title_base = rights.get("title") if isinstance(rights.get("title"), str) else ""
    author = rights.get("author") if isinstance(rights.get("author"), str) else ""
    title_base = title_base.strip()
    author = author.strip()
    if not title_base and isinstance(manifest.get("title"), str):
        title_base = manifest["title"].strip()
    if not author and isinstance(manifest.get("author"), str):
        author = manifest["author"].strip()
    if title_base and author:
        title = f"{title_base} — single-sale digital art"
        attribution = f"{title_base} by {author}."
    elif title_base:
        title = f"{title_base} — single-sale digital art"
        attribution = title_base
    else:
        title = "Single-sale digital art download"
        attribution = "Single-sale digital art."
    title = title[:MAX_TITLE]
    size_line = _intended_size_line(manifest)
    poem_body = ""
    if poem_text_cleared_for_reproduction(manifest):
        if isinstance(rights.get("body"), str) and rights["body"].strip():
            poem_body = rights["body"].strip()
        elif isinstance(manifest.get("source_text"), str) and manifest["source_text"].strip():
            poem_body = str(manifest["source_text"]).strip()
    lines = [
        attribution,
        "",
        DIGITAL_NOTICE,
        SINGLE_SALE_NOTICE,
    ]
    if size_line:
        lines.extend(["", size_line])
    if poem_body:
        lines.extend(["", POEM_INTRO, "", poem_body])
    lines.extend(["", AI_CUSTOMER_DISCLOSURE])
    tags = _tags(manifest, rights)
    return {
        "title": title,
        "description": "\n".join(lines).strip(),
        "tags": tags,
        "materials": list(DEFAULT_MATERIALS),
        "who_made": "i_did",
        "when_made": "2020_2026",
        "is_supply": False,
        "type": "download",
        "quantity": 1,
        "should_auto_renew": False,
    }


def _intended_size_line(manifest: dict) -> str:
    intended = (
        manifest.get("intended_print_size")
        if isinstance(manifest.get("intended_print_size"), dict)
        else None
    )
    if intended and intended.get("width_inches") is not None and intended.get("height_inches") is not None:
        width = _format_inches(intended.get("width_inches"))
        height = _format_inches(intended.get("height_inches"))
        ppi = intended.get("ppi")
        if ppi is not None:
            return f"Intended print size: {width}×{height} inches at {int(ppi) if float(ppi) == int(ppi) else ppi} PPI."
        return f"Intended print size: {width}×{height} inches."
    pixels = manifest.get("print_pixels") if isinstance(manifest.get("print_pixels"), dict) else {}
    width = pixels.get("width")
    height = pixels.get("height")
    if width and height:
        # Fallback when older packs lack intended_print_size.
        return f"Print file: {width}×{height} pixels (square digital download)."
    return ""


def _format_inches(value: object) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return str(int(number))
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text


def digital_files(folder: Path) -> list[Path]:
    """Downloadable print master only — never the preview image."""
    print_png = folder / "etsy" / "print.png"
    if not print_png.is_file():
        raise EtsyError("No digital print PNG is present in the pack.", kind="media")
    size = print_png.stat().st_size
    if size > MAX_DIGITAL_FILE_BYTES:
        raise EtsyError(
            f"The print file is {size} bytes; Etsy allows at most {MAX_DIGITAL_FILE_BYTES} bytes per file.",
            kind="media",
        )
    return [print_png]


def preview_image(folder: Path) -> Path:
    path = folder / "etsy" / "preview.png"
    if not path.is_file():
        raise EtsyError("The Etsy preview PNG is missing.", kind="media")
    return path


def _tags(manifest: dict, rights: dict) -> list[str]:
    tags = ["digital download", "art print", "wall art", "single edition"]
    orientation = manifest.get("orientation")
    if isinstance(orientation, str) and orientation:
        tags.append(orientation[:MAX_TAG_LEN])
    preset = manifest.get("style_preset")
    if isinstance(preset, str) and preset and preset != "none":
        tags.append(preset.replace("_", " ")[:MAX_TAG_LEN])
    abstraction = manifest.get("abstraction")
    if isinstance(abstraction, str) and abstraction:
        tags.append(abstraction[:MAX_TAG_LEN])
    author = rights.get("author")
    if isinstance(author, str) and author.strip():
        tags.append(author.strip()[:MAX_TAG_LEN])
    tags.append("AI generated"[:MAX_TAG_LEN])
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        value = "".join(ch for ch in tag if ch.isalnum() or ch in " -'").strip()
        if not value:
            continue
        value = value[:MAX_TAG_LEN]
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
        if len(cleaned) >= MAX_TAGS:
            break
    return cleaned
