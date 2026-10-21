"""Read a JPEG size without adding a dependency."""

from __future__ import annotations

from pathlib import Path

from anskar.instagram.errors import InstagramError

# Instagram feed photo limits used by the content publishing API.
MIN_SIDE = 320
MIN_ASPECT = 0.8  # 4:5
MAX_ASPECT = 1.91
MAX_BYTES = 8 * 1024 * 1024
MAX_LONG_EDGE = 1440


def jpeg_size(path: Path) -> tuple[int, int]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InstagramError("The JPEG could not be read.", kind="media") from exc
    return jpeg_dimensions(data)


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise InstagramError("The image is not a JPEG.", kind="media")
    position = 2
    while position + 3 < len(data):
        if data[position] != 0xFF:
            raise InstagramError("The JPEG structure is not readable.", kind="media")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA or position + 1 >= len(data):
            break
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            raise InstagramError("The JPEG structure is not readable.", kind="media")
        if marker in {0xC0, 0xC1, 0xC2}:
            block = data[position + 2 : position + length]
            if len(block) < 5:
                raise InstagramError("The JPEG structure is not readable.", kind="media")
            height = int.from_bytes(block[1:3], "big")
            width = int.from_bytes(block[3:5], "big")
            if width < 1 or height < 1:
                raise InstagramError("The JPEG has no pixel size.", kind="media")
            return width, height
        position += length
    raise InstagramError("The JPEG does not record a pixel size.", kind="media")


def validate_jpeg(path: Path) -> tuple[int, int]:
    """Reject a file Instagram's feed publisher would not accept."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InstagramError("The JPEG could not be read.", kind="media") from exc
    if size <= 0:
        raise InstagramError("The JPEG is empty.", kind="media")
    if size > MAX_BYTES:
        raise InstagramError("The JPEG is larger than Instagram's 8 MB limit.", kind="media")
    width, height = jpeg_size(path)
    if width < MIN_SIDE or height < MIN_SIDE:
        raise InstagramError(
            f"The JPEG is {width}×{height}. Instagram requires at least {MIN_SIDE} pixels on each side.",
            kind="media",
        )
    aspect = width / height
    if aspect < MIN_ASPECT or aspect > MAX_ASPECT:
        raise InstagramError(
            f"The JPEG aspect {aspect:.3f} is outside Instagram's range of 4:5 through 1.91:1.",
            kind="media",
        )
    if max(width, height) > MAX_LONG_EDGE:
        raise InstagramError(
            f"The JPEG long edge is {max(width, height)}. Instagram preparation keeps it at {MAX_LONG_EDGE} or below.",
            kind="media",
        )
    return width, height
