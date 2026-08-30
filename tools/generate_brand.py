"""Generate the original HACS brand icons without external image dependencies.

The output path is not a matter of taste. `hacs/action` looks for
`custom_components/<domain>/brand/icon.png` — `brand` singular, inside the
integration directory — and only falls back to the home-assistant/brands
repository when that is missing. Its own log says so:

    The repository does not contain brands assets at
    custom_components/atmoph_window/brand/icon.png. Falling back to checking
    the brands repository.

A root-level `brands/` directory, which is what the convention looks like from
the outside, is never read.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = ROOT / "custom_components" / "atmoph_window" / "brand"


def _png(path: Path, size: int) -> None:
    scale = size / 256
    pixels = bytearray(size * size * 4)

    def paint(x: int, y: int, color: tuple[int, int, int, int]) -> None:
        if 0 <= x < size and 0 <= y < size:
            offset = (y * size + x) * 4
            pixels[offset : offset + 4] = bytes(color)

    def rectangle(
        left: float,
        top: float,
        right: float,
        bottom: float,
        color: tuple[int, int, int, int],
    ) -> None:
        for y in range(round(top * scale), round(bottom * scale)):
            for x in range(round(left * scale), round(right * scale)):
                paint(x, y, color)

    blue = (25, 118, 210, 255)
    sky = (130, 205, 255, 255)
    white = (255, 255, 255, 255)

    # A distinctive blue frame with four sky panes.
    rectangle(28, 18, 196, 238, blue)
    rectangle(45, 37, 104, 119, sky)
    rectangle(120, 37, 179, 119, sky)
    rectangle(45, 135, 104, 219, sky)
    rectangle(120, 135, 179, 219, sky)

    # BLE radio arcs beside the frame.
    thickness = max(2, round(5 * scale))
    for radius in (22, 38, 54):
        center_x = round(186 * scale)
        center_y = round(128 * scale)
        for y in range(round((128 - radius) * scale), round((128 + radius) * scale)):
            for x in range(center_x, round((186 + radius) * scale)):
                distance = ((x - center_x) ** 2 + (y - center_y) ** 2) ** 0.5
                if abs(distance - radius * scale) <= thickness / 2:
                    paint(x, y, white)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = b"".join(
        b"\x00" + bytes(pixels[y * size * 4 : (y + 1) * size * 4]) for y in range(size)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    _png(BRAND_DIR / "icon.png", 256)
    _png(BRAND_DIR / "icon@2x.png", 512)
    # home-assistant/brands wants a logo too; a square logo is acceptable when
    # the mark has no wide form, and HACS only requires the icon.
    _png(BRAND_DIR / "logo.png", 256)
    _png(BRAND_DIR / "logo@2x.png", 512)
