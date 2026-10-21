"""Build Etsy listing fields from a publish pack manifest."""

from __future__ import annotations

import json
from pathlib import Path

from anskar.etsy.errors import EtsyError
from anskar.printmaker import AI_DISCLOSURE, DIGITAL_NOTICE

MAX_TITLE = 140
MAX_TAGS = 13
MAX_TAG_LEN = 20


def load_pack(pack_id: str, *, packs: Path) -> tuple[Path, dict]:
    if not pack_id or "/" in pack_id or "\\" in pack_id or Path(pack_id).name != pack_id:
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


def listing_fields(manifest: dict) -> dict[str, object]:
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
    title_base = rights.get("title") if isinstance(rights.get("title"), str) else ""
    author = rights.get("author") if isinstance(rights.get("author"), str) else ""
    title_base = title_base.strip()
    author = author.strip()
    if title_base and author:
        title = f"{title_base} — digital art download"
        attribution = f"{title_base} by {author}."
    elif title_base:
        title = f"{title_base} — digital art download"
        attribution = title_base
    else:
        title = "Digital art download"
        attribution = "Artwork without a documented poem title."
    title = title[:MAX_TITLE]
    pixels = manifest.get("print_pixels") if isinstance(manifest.get("print_pixels"), dict) else {}
    width = pixels.get("width")
    height = pixels.get("height")
    intended = manifest.get("intended_print_size") if isinstance(manifest.get("intended_print_size"), dict) else None
    size_line = ""
    if intended:
        size_line = (
            f"Intended print size: {intended.get('width_inches')}×{intended.get('height_inches')} inches "
            f"at {intended.get('ppi')} PPI.\n"
        )
    description = "\n".join(
        [
            attribution,
            "",
            DIGITAL_NOTICE,
            "",
            f"Image dimensions: {width}×{height} pixels." if width and height else "Digital art print file.",
            size_line.rstrip(),
            "The download uses the approved pixels. They were not upscaled.",
            "",
            AI_DISCLOSURE,
            "",
            str(manifest.get("ai_disclosure") or "").strip(),
        ]
    ).strip()
    tags = _tags(manifest, rights)
    return {
        "title": title,
        "description": description,
        "tags": tags,
        "who_made": "i_did",
        "when_made": "2020_2025",
        "is_supply": False,
        "type": "download",
    }


def digital_files(folder: Path) -> list[Path]:
    files: list[Path] = []
    print_png = folder / "etsy" / "print.png"
    print_jpg = folder / "etsy" / "print.jpg"
    if print_png.is_file():
        files.append(print_png)
    if print_jpg.is_file():
        files.append(print_jpg)
    if not files:
        raise EtsyError("No digital print files are present in the pack.", kind="media")
    return files


def preview_image(folder: Path) -> Path:
    path = folder / "etsy" / "preview.png"
    if not path.is_file():
        raise EtsyError("The Etsy preview PNG is missing.", kind="media")
    return path


def _tags(manifest: dict, rights: dict) -> list[str]:
    tags = ["digital download", "art print", "wall art"]
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
