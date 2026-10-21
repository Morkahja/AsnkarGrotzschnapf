"""Read a PNG pixel size, and describe the print that size can support.

A DPI label is metadata. Print quality is the pixel count divided by the
intended physical side.
"""

import math
import struct
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TARGET_PPI = 200
MINIMUM_PPI = 150
EXCELLENT_PPI = 300
SQUARE_INCHES = (4, 6, 8, 10, 12)
DEFAULT_PRINT_INCHES = 8
# The generator may still be asked for this many pixels. It is 12 inches at 200 PPI.
PRINT_LONG_EDGE = TARGET_PPI * SQUARE_INCHES[-1]
PRINT_DPI = EXCELLENT_PPI
PRINT_INCHES = 8


def pixels_for_print(
    width_inches: float,
    height_inches: float,
    ppi: int = TARGET_PPI,
) -> tuple[int, int]:
    """Pixel size for a physical print at the target PPI.

    Each side is rounded up to a multiple of 8. The result is never smaller
    than the requested print.
    """
    if ppi < 1:
        raise ValueError("PPI must be positive.")
    return _print_edge(width_inches, ppi), _print_edge(height_inches, ppi)


def _print_edge(inches: float, ppi: int) -> int:
    if isinstance(inches, bool) or not isinstance(inches, (int, float)) or inches <= 0:
        raise ValueError("Print size must be a positive number of inches.")
    raw = float(inches) * ppi
    return int(math.ceil(raw / 8.0) * 8)


def intended_print(width: int, height: int, ppi: int = TARGET_PPI) -> dict[str, int] | None:
    """Inches these pixels cover exactly at the target PPI, when both sides divide evenly."""
    if width < 1 or height < 1 or ppi < 1:
        return None
    if width % ppi or height % ppi:
        return None
    return {"width_inches": width // ppi, "height_inches": height // ppi, "ppi": ppi}


def effective_ppi(pixels: int, inches: float) -> int:
    """Whole PPI those pixels provide across one physical side."""
    if inches <= 0:
        raise ValueError("Inches must be positive.")
    if pixels < 1:
        raise ValueError("Pixels must be positive.")
    return int(round(pixels / inches))


def effective_dpi(pixels: int, inches: float) -> float:
    """One-decimal PPI. Prefer effective_ppi for a print classification."""
    if inches <= 0:
        raise ValueError("Inches must be positive.")
    return round(pixels / inches, 1)


def classify_ppi(ppi: int) -> dict[str, object]:
    """Map a real PPI figure to the print standard. The figure is not a file label."""
    if ppi >= EXCELLENT_PPI:
        quality, label = "high", "Excellent"
    elif ppi >= TARGET_PPI:
        quality, label = "standard", "Recommended"
    elif ppi >= MINIMUM_PPI:
        quality, label = "artistic", "Acceptable"
    else:
        quality, label = "low", "Not print-ready"
    return {
        "classification": quality,
        "classification_label": label,
        "suitable": ppi >= MINIMUM_PPI,
        "recommended": ppi >= TARGET_PPI,
    }


def print_sizes(width: int, height: int) -> list[dict[str, object]]:
    """Supported sizes for these pixels. Non-square art keeps its shape."""
    if width < 1 or height < 1:
        raise ValueError("The artwork has no pixel size.")
    square = width == height
    long_pixels = max(width, height)
    short_pixels = min(width, height)
    sizes = []
    for long_inches in SQUARE_INCHES:
        short_inches = long_inches * short_pixels / long_pixels
        if width >= height:
            width_inches, height_inches = float(long_inches), short_inches
        else:
            width_inches, height_inches = short_inches, float(long_inches)
        ppi = effective_ppi(long_pixels, long_inches)
        sizes.append(
            {
                "long_inches": long_inches,
                "width_inches": round(width_inches, 2),
                "height_inches": round(height_inches, 2),
                "square": square,
                "ppi": ppi,
                **classify_ppi(ppi),
            }
        )
    return sizes


def largest_size(sizes: list[dict[str, object]], *, recommended: bool) -> dict[str, object] | None:
    key = "recommended" if recommended else "suitable"
    matches = [size for size in sizes if size[key]]
    if not matches:
        return None
    return max(matches, key=lambda size: int(size["long_inches"]))


def print_sized(width: int, height: int) -> bool:
    """True when at least one supported size meets 150 PPI. 300 PPI is not required."""
    return any(bool(size["suitable"]) for size in print_sizes(width, height))


def physical_inches(width: int, height: int, dpi: int = PRINT_DPI) -> tuple[float, float]:
    """Inches a DPI label would imply. The label adds no pixels."""
    return (round(width / dpi, 2), round(height / dpi, 2))


def png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) < 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError(f"Not a PNG image: {path}")
    width, height = struct.unpack(">II", header[16:24])
    if width < 1 or height < 1:
        raise ValueError(f"PNG has no pixel size: {path}")
    return width, height


def label_png_dpi(path: Path, dpi: int = 300) -> None:
    """Record a print label. The IHDR pixel size stays the same."""
    raw = path.read_bytes()
    if not raw.startswith(PNG_SIGNATURE):
        raise ValueError(f"Not a PNG image: {path}")
    pixels_per_meter = round(dpi / 0.0254)
    label = _chunk(b"pHYs", struct.pack(">IIB", pixels_per_meter, pixels_per_meter, 1))
    pieces = [raw[:8]]
    position = 8
    inserted = False
    while position + 12 <= len(raw):
        length = struct.unpack(">I", raw[position : position + 4])[0]
        tag = raw[position + 4 : position + 8]
        end = position + 12 + length
        chunk = raw[position:end]
        position = end
        if tag == b"pHYs":
            continue
        if tag == b"IEND" and not inserted:
            pieces.append(label)
            inserted = True
        pieces.append(chunk)
        if tag == b"IEND":
            break
    if not inserted:
        raise ValueError(f"PNG has no end marker: {path}")
    path.write_bytes(b"".join(pieces))


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)
