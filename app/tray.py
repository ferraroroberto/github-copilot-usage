"""Optional Windows/macOS system-tray launcher (pystray).

Runs the uvicorn server in a background thread and puts a small icon in the
system tray with Open dashboard / Open on private network (only while Tailscale
access is live) / Restart server / Quit. Entirely optional:
the dashboard works identically via ``python -m app``; this only exists so
the tool can live quietly in the tray all day.

Requires the extras in ``requirements-tray.txt`` (pystray + pillow) —
``tray.bat`` installs them on first use.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import webbrowser
from pathlib import Path
from typing import Optional

# Under pythonw.exe there is no console: sys.stdout/stderr are None, and
# anything that touches them (uvicorn's logging setup calls
# sys.stdout.isatty(), pystray/library error prints) crashes the process.
# Bind them to a null sink before any such import or call.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, io.TextIOWrapper(open(os.devnull, "wb"), encoding="utf-8"))

try:
    import pystray
    from PIL import Image
except ImportError:
    print("❌ tray extras missing — run: pip install -r requirements-tray.txt")
    sys.exit(1)

from app.listeners import Listeners
from src import config

logger = logging.getLogger(__name__)


_ICON_PATH = Path(__file__).resolve().parent / "webapp" / "static" / "icon-512.png"


def _make_icon_image() -> "Image.Image":
    """The tray icon is the web app icon itself, so the two cannot drift apart.

    A missing or unreadable file falls back to a plain near-black tile: the tray
    must still start without its icon art.
    """
    try:
        with Image.open(_ICON_PATH) as src:
            return src.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        logger.warning("⚠️ tray icon art %s unreadable; using a plain tile", _ICON_PATH)
        return Image.new("RGB", (64, 64), (10, 10, 10))


def main() -> None:
    port = int(config.load().get("port", 8377))
    icon: Optional["pystray.Icon"] = None

    def redraw_menu() -> None:
        # Windows rebuilds the menu on every right-click; other backends keep the last one drawn.
        if icon is not None:
            icon.update_menu()

    def start_listeners() -> Listeners:
        listeners = Listeners(port)
        listeners.on_tailnet_change = redraw_menu
        listeners.start()
        return listeners

    server = start_listeners()
    url = server.loopback_url

    def on_open(icon, item) -> None:  # noqa: ANN001 - pystray callback shape
        webbrowser.open(url)

    def tailnet_live(item) -> bool:  # noqa: ANN001
        return server.tailnet_url is not None

    def on_open_tailnet(icon, item) -> None:  # noqa: ANN001
        tailnet_url = server.tailnet_url
        if tailnet_url is not None:
            webbrowser.open(tailnet_url)

    def on_restart(icon, item) -> None:  # noqa: ANN001
        nonlocal server
        server.stop()
        server = start_listeners()
        redraw_menu()

    def on_quit(icon, item) -> None:  # noqa: ANN001
        server.stop()
        icon.stop()

    icon = pystray.Icon(
        "github-copilot-usage",
        _make_icon_image(),
        "Copilot Usage — " + url,
        menu=pystray.Menu(
            pystray.MenuItem("Open dashboard", on_open, default=True),
            pystray.MenuItem("Open on private network", on_open_tailnet, visible=tailnet_live),
            pystray.MenuItem("Restart server", on_restart),
            pystray.MenuItem("Quit", on_quit),
        ),
    )
    icon.run()


if __name__ == "__main__":
    main()
