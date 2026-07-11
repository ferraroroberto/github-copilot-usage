"""Optional Windows/macOS system-tray launcher (pystray).

Runs the uvicorn server in a background thread and puts a small icon in the
system tray with Open dashboard / Restart server / Quit. Entirely optional:
the dashboard works identically via ``python -m app``; this only exists so
the tool can live quietly in the tray all day.

Requires the extras in ``requirements-tray.txt`` (pystray + pillow) —
``tray.bat`` installs them on first use.
"""

from __future__ import annotations

import sys
import threading
import webbrowser

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("❌ tray extras missing — run: pip install -r requirements-tray.txt")
    sys.exit(1)

import uvicorn

from app import config


def _make_icon_image() -> "Image.Image":
    """A simple generated glyph: rounded blue square with a white gauge arc."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 2, size - 2], radius=14, fill=(9, 105, 218, 255))
    d.arc([12, 12, size - 12, size - 12], start=130, end=320, fill=(255, 255, 255, 255), width=7)
    d.ellipse([size // 2 - 4, size // 2 - 4, size // 2 + 4, size // 2 + 4], fill=(255, 255, 255, 255))
    return img


class _Server:
    def __init__(self, port: int) -> None:
        self.port = port
        self._server: uvicorn.Server = None  # type: ignore[assignment]
        self._thread: threading.Thread = None  # type: ignore[assignment]

    def start(self) -> None:
        cfg = uvicorn.Config("app.server:app", host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


def main() -> None:
    port = int(config.load().get("port", 8377))
    url = "http://127.0.0.1:" + str(port) + "/"
    server = _Server(port)
    server.start()

    def on_open(icon, item) -> None:  # noqa: ANN001 - pystray callback shape
        webbrowser.open(url)

    def on_restart(icon, item) -> None:  # noqa: ANN001
        server.stop()
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
