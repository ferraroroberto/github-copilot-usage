"""Entry point: ``python -m app`` starts the dashboard server.

Usage:
    python -m app                # serve on config port (default 8377)
    python -m app --port 9000    # override the port
    python -m app --open         # also open the dashboard in the browser
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from app.listeners import Listeners
from src import config

# A stated guess at uvicorn's bind time, not a measurement: opening the browser
# earlier can hit a refused connection (a manual reload then recovers).
_BROWSER_OPEN_DELAY_SECS = 1.5


def main() -> None:
    # Windows consoles/captured pipes default to cp1252, which cannot encode
    # the emoji breadcrumbs; force UTF-8 so output never crashes the app.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="github-copilot-usage")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default: config.json / 8377)")
    parser.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    args = parser.parse_args()

    port = args.port or int(config.load().get("port", 8377))
    listeners = Listeners(port, log_level="info")
    url = listeners.loopback_url

    if args.open:
        threading.Timer(_BROWSER_OPEN_DELAY_SECS, webbrowser.open, args=(url,)).start()

    listeners.start()
    print("ℹ️ github-copilot-usage — dashboard at " + url)
    if listeners.tailnet_url:
        print("ℹ️ also open from your Tailscale network at " + listeners.tailnet_url)
    try:
        listeners.wait()
    except KeyboardInterrupt:
        listeners.stop()


if __name__ == "__main__":
    main()
