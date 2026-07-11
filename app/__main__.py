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

import uvicorn

from app import config


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
    url = "http://127.0.0.1:" + str(port) + "/"

    if args.open:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()

    print("ℹ️ github-copilot-usage — dashboard at " + url)
    uvicorn.run("app.server:app", host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
