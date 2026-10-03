"""
scripts/make_icons.py — generate the PWA/app icons.

WHY A GENERATOR INSTEAD OF CHECKED-IN BINARIES
    Every other asset in this repo is source you can read. Binary PNGs are
    the one place a reviewer has to trust a blob, and they go stale silently
    (someone edits the palette, forgets the icon). So the icons are drawn
    from code — no Pillow dependency, just zlib and struct, which keeps the
    Space image small.

THE DESIGN
    The dashboard HUD is a voice orb: a dark field, a luminous ring, a bright
    core. The icon is the same object, so the installed app and the running
    app read as one thing. Maskable icons keep the art inside the 80% safe
    circle Android crops to; the plain ones fill the square.

    python3 scripts/make_icons.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "dashboard" / "static"

BG_TOP = (11, 18, 32)       # near-black navy
BG_BOT = (22, 35, 61)
RING_IN = (56, 189, 248)    # cyan-400
RING_OUT = (99, 102, 241)   # indigo-500
CORE = (240, 253, 255)      # white with a cyan cast


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int],
          t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def _blend(dst: tuple[int, int, int], src: tuple[int, int, int],
           alpha: float) -> tuple[int, int, int]:
    return _lerp(dst, src, max(0.0, min(1.0, alpha)))


def _rounded_alpha(x: float, y: float, size: float, radius: float) -> float:
    """Coverage of a rounded square, anti-aliased over one pixel."""
    half = size / 2.0
    cx = min(max(x, half - radius), size - half + radius)
    cy = min(max(y, half - radius), size - half + radius)
    d = math.hypot(x - cx, y - cy)
    return max(0.0, min(1.0, radius - d + 0.5))


def _ring_alpha(dist: float, r_in: float, r_out: float) -> float:
    """Coverage of an annulus, anti-aliased at both edges."""
    a_in = min(1.0, max(0.0, dist - r_in + 0.5))
    a_out = min(1.0, max(0.0, r_out - dist + 0.5))
    return max(0.0, min(a_in, a_out))


def _core_alpha(dist: float, r: float) -> float:
    return max(0.0, min(1.0, r - dist + 0.5))


def render(size: int, *, maskable: bool = False) -> bytes:
    """Render RGBA pixels, then pack them into a PNG."""
    # Maskable icons get cropped to a circle of 80% width — shrink the art so
    # the ring never touches the crop edge.
    art = size * (0.62 if maskable else 0.78)
    c = size / 2.0
    radius = size * (0.5 if maskable else 0.22)
    r_out = art / 2.0
    r_in = r_out * 0.72
    r_core = r_out * 0.30
    rows = bytearray()
    for py in range(size):
        rows.append(0)                      # PNG filter byte: none
        y = py + 0.5
        for px in range(size):
            x = px + 0.5
            t = y / size
            px_rgb = _lerp(BG_TOP, BG_BOT, t)
            a = _rounded_alpha(x, y, size, radius)
            if maskable:
                # Full bleed: Android supplies its own mask, so no rounding.
                a = 1.0
            d = math.hypot(x - c, y - c)
            ring = _ring_alpha(d, r_in, r_out)
            if ring > 0:
                # Ring colour sweeps cyan→indigo with the angle.
                ang = (math.atan2(y - c, x - c) + math.pi) / (2 * math.pi)
                px_rgb = _blend(px_rgb, _lerp(RING_IN, RING_OUT, ang), ring)
            core = _core_alpha(d, r_core)
            if core > 0:
                px_rgb = _blend(px_rgb, CORE, core)
            rows.extend((px_rgb[0], px_rgb[1], px_rgb[2], int(255 * a)))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    targets = [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
        # iOS looks for apple-touch-icon.png at the site root; every other icon
        # keeps the icon-*.png family under /static.
        ("apple-touch-icon.png", 180, False),
    ]
    for name, size, maskable in targets:
        png = render(size, maskable=maskable)
        (OUT / name).write_bytes(png)
        print(f"  {name}  {size}x{size}  {len(png) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
