"""Part 6C-B: deterministic extension icon generation.

Generates the 4 required Chrome extension icon sizes (16/32/48/128) as real PNG files
using ONLY the Python standard library (zlib for compression/CRC, struct for binary
packing) - no Pillow, no cairosvg, no third-party dependency, and no network fetch of
any remote asset.

The design is intentionally restrained and original: a solid rounded square in the
extension's own existing institutional accent color (`--accent: #1f4d3a` from
extension/css/sidepanel.css) with a simple ascending three-bar "research/analytics"
glyph in white - a neutral mark that does not imitate any third-party brand. The same
proportions are described in extension/icons/source.svg for reference/future editing;
this script does not parse that SVG (there is no stdlib SVG rasterizer) - it draws the
equivalent shapes directly in a pixel grid at each target size, so the two are kept in
sync by design intent and documented here, not by one being derived from the other.

Usage: uv run python scripts/generate_icons.py [--output-dir extension/icons]
"""

import argparse
import struct
import zlib
from pathlib import Path

ACCENT_RGB = (0x1F, 0x4D, 0x3A)
WHITE_RGB = (0xFF, 0xFF, 0xFF)
SIZES = (16, 32, 48, 128)


def _corner_radius(size: int) -> float:
    return size * 0.18


def _rounded_square_mask(size: int, radius: float) -> list[list[bool]]:
    mask = [[False] * size for _ in range(size)]
    for y in range(size):
        for x in range(size):
            # Distance-to-nearest-corner-center test for a rounded square; interior
            # (non-corner) pixels are always inside.
            cx = min(max(x + 0.5, radius), size - radius)
            cy = min(max(y + 0.5, radius), size - radius)
            dx = (x + 0.5) - cx
            dy = (y + 0.5) - cy
            mask[y][x] = (dx * dx + dy * dy) <= radius * radius
    return mask


def _bar_glyph_mask(size: int) -> list[list[bool]]:
    """Three ascending bars, centered, occupying the middle ~60% of the icon -
    legible even at 16px."""
    mask = [[False] * size for _ in range(size)]
    margin = size * 0.22
    usable = size - 2 * margin
    bar_count = 3
    gap = usable * 0.12
    bar_width = (usable - gap * (bar_count - 1)) / bar_count
    heights = [usable * 0.42, usable * 0.68, usable * 1.0]
    baseline = size - margin
    for i, height in enumerate(heights):
        x0 = margin + i * (bar_width + gap)
        x1 = x0 + bar_width
        y0 = baseline - height
        y1 = baseline
        for y in range(size):
            if not (y0 <= y + 0.5 <= y1):
                continue
            for x in range(size):
                if x0 <= x + 0.5 <= x1:
                    mask[y][x] = True
    return mask


def render_icon_pixels(size: int) -> list[list[tuple[int, int, int, int]]]:
    background = _rounded_square_mask(size, _corner_radius(size))
    glyph = _bar_glyph_mask(size)
    pixels: list[list[tuple[int, int, int, int]]] = []
    for y in range(size):
        row: list[tuple[int, int, int, int]] = []
        for x in range(size):
            if glyph[y][x] and background[y][x]:
                row.append((*WHITE_RGB, 255))
            elif background[y][x]:
                row.append((*ACCENT_RGB, 255))
            else:
                row.append((0, 0, 0, 0))
        pixels.append(row)
    return pixels


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, pixels: list[list[tuple[int, int, int, int]]]) -> None:
    height = len(pixels)
    width = len(pixels[0]) if height else 0
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0 (none) for this scanline
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA, color type 6
    idat = zlib.compress(bytes(raw), level=9)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def generate_all(output_dir: Path, sizes: tuple[int, ...] = SIZES) -> list[Path]:
    written = []
    for size in sizes:
        path = output_dir / f"icon{size}.png"
        write_png(path, render_icon_pixels(size))
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "extension" / "icons",
    )
    args = parser.parse_args()
    written = generate_all(args.output_dir)
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
