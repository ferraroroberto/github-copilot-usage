"""Optional Windows/macOS system-tray launcher (pystray).

Runs the uvicorn server in a background thread and puts a small icon in the
system tray with Open dashboard / Restart server / Quit. Entirely optional:
the dashboard works identically via ``python -m app``; this only exists so
the tool can live quietly in the tray all day.

Requires the extras in ``requirements-tray.txt`` (pystray + pillow) —
``tray.bat`` installs them on first use.
"""

from __future__ import annotations

import io
import os
import sys
import webbrowser

# Under pythonw.exe there is no console: sys.stdout/stderr are None, and
# anything that touches them (uvicorn's logging setup calls
# sys.stdout.isatty(), pystray/library error prints) crashes the process.
# Bind them to a null sink before any such import or call.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, io.TextIOWrapper(open(os.devnull, "wb"), encoding="utf-8"))

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("❌ tray extras missing — run: pip install -r requirements-tray.txt")
    sys.exit(1)

from app.listeners import Listeners
from src import config


def _make_icon_image() -> "Image.Image":
    """A simple generated glyph: rounded blue square with a white gauge arc."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 2, size - 2], radius=14, fill=(9, 105, 218, 255))
    d.arc([12, 12, size - 12, size - 12], start=130, end=320, fill=(255, 255, 255, 255), width=7)
    d.ellipse([size // 2 - 4, size // 2 - 4, size // 2 + 4, size // 2 + 4], fill=(255, 255, 255, 255))
    return img


def main() -> None:
    port = int(config.load().get("port", 8377))
    server = Listeners(port)
    server.start()
    url = server.loopback_url

    def on_open(icon, item) -> None:  # noqa: ANN001 - pystray callback shape
        webbrowser.open(url)

    def on_restart(icon, item) -> None:  # noqa: ANN001
        nonlocal server
        server.stop()
        server = Listeners(port)
        server.start()

    def on_quit(icon, item) -> None:  # noqa: ANN001
        server.stop()
        icon.stop()

    icon = pystray.Icon(
        "github-copilot-usage",
        _make_icon_image(),
        "Copilot Usage — " + url,
        menu=pystray.Menu(
            pystray.MenuItem("Open dashboard", on_open, default=True),
            pystray.MenuItem("Restart server", on_restart),
            pystray.MenuItem("Quit", on_quit),
        ),
    )
    icon.run()


if __name__ == "__main__":
    main()
