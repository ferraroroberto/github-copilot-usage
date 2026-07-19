"""Self-contained PWA / favicon asset generator for github-copilot-usage.

Implements the fleet's ``brand_gen`` app-icon contract (design.md ``app-icon``:
one brand master -> the canonical raster set ``icon-180/192/512``, a distinct
``icon-512-maskable`` with Android safe-zone padding, and a multi-size
``favicon.ico``) **without importing project-scaffolding's shared
``brand_gen`` generator** — this repo ships publicly to locked-down corporate
machines, so it may not reference any other local repo or private infra
(see CLAUDE.md "Standalone by design").

The brand master is this app's own ``static/favicon.svg`` — a GitHub-blue
rounded tile behind a three-bar column-chart glyph (the same Lucide
``chart-column`` mark used for the in-app ``.logo``). Its geometry is
reproduced here with Pillow drawing primitives, so no SVG renderer is needed;
Pillow is already a repo dependency (``requirements-tray.txt``), so this adds
no new footprint. The generated PNG/ICO assets are committed, so nothing at
runtime ever imports this script.

Regenerate after changing the brand:

    .venv\\Scripts\\python.exe scripts\\gen_icons.py
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from PIL import Image, ImageDraw

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# --- brand master geometry (static/favicon.svg, a 64-unit design grid) ---
BRAND_BLUE = (9, 105, 218)      # #0969da — the GitHub-blue accent tile
BAR_WHITE = (255, 255, 255)
_STROKE = 6.0                    # bar stroke width in grid units
# (centre-x, y-top, y-bottom) for each column, round-capped
_BARS: List[Tuple[float, float, float]] = [
    (18.0, 30.0, 44.0),
    (32.0, 18.0, 44.0),
    (46.0, 36.0, 44.0),
]
# glyph extent including the round caps (half-stroke = 3 units)
_GLYPH_BOX = (15.0, 15.0, 49.0, 47.0)  # minx, miny, maxx, maxy

# padding of the glyph inside the opaque tile
FULL_BLEED_PAD = 0.16   # regular / apple / favicon
MASKABLE_PAD = 0.26     # Android adaptive-icon safe zone


def _render_tile(size: int, pad_ratio: float) -> Image.Image:
    """Rasterize the brand glyph centred on an opaque full-bleed blue tile.

    Opaque RGB (no alpha, no transparent corners) is required: iOS composites
    any alpha against black, and Android/iOS apply their own mask shape, so the
    source must be an edge-to-edge opaque square — the rounded-corner look lives
    in the vector ``favicon.svg`` and in the platform mask, never baked in here.
    """
    img = Image.new("RGB", (size, size), BRAND_BLUE)
    draw = ImageDraw.Draw(img)

    gx0, gy0, gx1, gy1 = _GLYPH_BOX
    glyph_w, glyph_h = gx1 - gx0, gy1 - gy0
    avail = size * (1 - 2 * pad_ratio)
    scale = avail / max(glyph_w, glyph_h)
    off_x = (size - glyph_w * scale) / 2 - gx0 * scale
    off_y = (size - glyph_h * scale) / 2 - gy0 * scale

    radius = (_STROKE / 2) * scale
    for cx, y_top, y_bot in _BARS:
        x = cx * scale + off_x
        top = y_top * scale + off_y
        bot = y_bot * scale + off_y
        draw.rounded_rectangle(
            [x - radius, top - radius, x + radius, bot + radius],
            radius=radius,
            fill=BAR_WHITE,
        )
    return img


def render_set(out_dir: Path = STATIC_DIR) -> None:
    """Emit the canonical app-icon family into ``out_dir``.

    Writes: ``icon-512.png``, ``icon-512-maskable.png``, ``icon-192.png``,
    ``icon-180.png`` and a multi-size ``favicon.ico`` (16/32/48).
    """
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
