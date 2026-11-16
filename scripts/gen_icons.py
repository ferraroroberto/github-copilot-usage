"""Self-contained PWA / favicon / tray asset generator for github-copilot-usage.

Implements the fleet's app-icon contract (one brand master -> the canonical
raster set ``icon-180/192/512``, a distinct ``icon-512-maskable`` with Android
safe-zone padding, and a multi-size ``favicon.ico``) **without importing any
shared generator** — this repo ships publicly to locked-down corporate
machines, so it may not reference any other local repo or private infra
(see CLAUDE.md "Standalone by design").

The brand master is this app's own ``app/webapp/static/favicon.svg``: the
Lucide ``chart-column`` glyph drawn as a light stroke on a near-black tile, the
look every fleet app shares. Its geometry is reproduced here with Pillow
primitives (no SVG renderer needed; Pillow is already a repo dependency via
``requirements-tray.txt``). The generated PNG/ICO assets are committed, and the
Windows tray loads ``icon-512.png`` at runtime, so the web icons and the tray
icon come from this one definition.

Regenerate after changing the brand:

    .venv\\Scripts\\python.exe scripts\\gen_icons.py
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import List, Tuple

from PIL import Image, ImageDraw

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "webapp" / "static"

TILE_BG = (10, 10, 10)          # #0A0A0A
GLYPH_STROKE = (240, 240, 240)  # #F0F0F0
STROKE_WIDTH = 2.6              # grid units on Lucide's 24-unit grid
GRID = 24.0

FULL_BLEED_PAD = 0.12   # regular / apple / favicon
MASKABLE_PAD = 0.26     # Android adaptive-icon safe zone
SUPERSAMPLE = 4         # Pillow draws aliased, so draw large and downsample

Point = Tuple[float, float]


def _axis_path() -> List[Point]:
    """Lucide ``M3 3v16a2 2 0 0 0 2 2h16``: the L-shaped axis with a rounded corner."""
    corner = [
        (5.0 - 2.0 * math.cos(math.radians(a)), 19.0 + 2.0 * math.sin(math.radians(a)))
        for a in range(0, 91, 3)
    ]
    return [(3.0, 3.0)] + corner + [(21.0, 21.0)]


# Every stroked path of the glyph, in grid units.
_PATHS: List[List[Point]] = [
    _axis_path(),
    [(18.0, 17.0), (18.0, 9.0)],
    [(13.0, 17.0), (13.0, 5.0)],
    [(8.0, 17.0), (8.0, 14.0)],
]


def _stroke(draw: ImageDraw.ImageDraw, points: List[Point], width: float) -> None:
    """Draw a round-capped, round-joined polyline of ``width`` pixels."""
    radius = width / 2
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        draw.line([(x0, y0), (x1, y1)], fill=GLYPH_STROKE, width=round(width))
    for x, y in points:
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=GLYPH_STROKE)


def _render_tile(size: int, pad_ratio: float) -> Image.Image:
    """Rasterize the glyph on an opaque full-bleed near-black tile.

    Opaque RGB (no alpha, no transparent corners) is required: iOS composites
    any alpha against black, and Android/iOS apply their own mask shape, so the
    source must be an edge-to-edge opaque square — the rounded-corner look lives
    in the vector ``favicon.svg`` and in the platform mask, never baked in here.
    """
    big = size * SUPERSAMPLE
    img = Image.new("RGB", (big, big), TILE_BG)
    draw = ImageDraw.Draw(img)

    scale = big * (1 - 2 * pad_ratio) / GRID
    offset = big * pad_ratio
    for path in _PATHS:
        _stroke(draw, [(offset + x * scale, offset + y * scale) for x, y in path], STROKE_WIDTH * scale)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def render_set(out_dir: Path = STATIC_DIR) -> None:
    """Emit the canonical app-icon family into ``out_dir``.

    Writes: ``icon-512.png``, ``icon-512-maskable.png``, ``icon-192.png``,
    ``icon-180.png`` and a multi-size ``favicon.ico`` (16/32/48).
    """
    # runtime-write-exempt: deliberately committed artefact, the PWA icons served from app/webapp/static (the manifest names these paths)
    out_dir.mkdir(parents=True, exist_ok=True)

    icon_512 = _render_tile(512, FULL_BLEED_PAD)
    icon_512.save(out_dir / "icon-512.png")

    _render_tile(512, MASKABLE_PAD).save(out_dir / "icon-512-maskable.png")

    icon_512.resize((192, 192), Image.Resampling.LANCZOS).save(out_dir / "icon-192.png")
    icon_512.resize((180, 180), Image.Resampling.LANCZOS).save(out_dir / "icon-180.png")

    _render_tile(256, FULL_BLEED_PAD).save(
        out_dir / "favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )


if __name__ == "__main__":
    render_set()
    print(f"wrote app-icon family to {STATIC_DIR}")
