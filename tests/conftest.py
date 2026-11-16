"""Shared test fixtures: the synthetic-record factory (no real logs, prompts or figures), an isolated config file, and a live server plus headless Chromium for the browser tests."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import Callable, Iterator

import pytest
import uvicorn

from src import config
from src.vscode_parser import RequestRecord

# Noon local time: a day-bucketed period test cannot straddle midnight.
TODAY_NOON = datetime.combine(date.today(), clock(12, 0)).astimezone()


@pytest.fixture
def tools_heavy() -> list:
    """A composition where tool definitions dominate and the user's own text is 10%."""
    return [
        {"category": "System", "label": "System Instructions", "percentageOfPrompt": 20},
        {"category": "System", "label": "Tool Definitions", "percentageOfPrompt": 70},
        {"category": "User Context", "label": "Messages", "percentageOfPrompt": 10},
    ]


@pytest.fixture
def rec() -> Callable[..., RequestRecord]:
    """``rec(n, **overrides)``: request ``n`` of a synthetic session, one minute apart, today."""

    def make(n: int = 0, days_ago: int = 0, **kw) -> RequestRecord:
        base = dict(
            ide="Code",
            source="vscode",
            project_key="e:\\alpha",
            project_name="alpha",
            project_path="E:\\alpha",
            session_id="sess-a",
            session_title="Alpha session",
            request_id="req-%03d" % n,
            ts=TODAY_NOON - timedelta(days=days_ago) + timedelta(minutes=n),
            message="prompt number %d" % n,
            mode="agent",
            model_requested="copilot/gpt-5-mini",
            model="gpt-5-mini",
            prompt_tokens=10_000,
            completion_tokens=100,
            credits=1.0,
            elapsed_ms=1000,
        )
        base.update(kw)
        return RequestRecord(**base)

    return make


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway config.json with the module's cache emptied before and after."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "_loaded", False)
    monkeypatch.setattr(config, "_cache", {})
    return path

@contextmanager
def _serve_app() -> Iterator[str]:
    """Run the dashboard on a free loopback port and yield its base URL.

    The caller has already pointed ``aggregate.gather_records`` at synthetic records.
    """
    from app.server import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "the test server did not start"
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def serve():
    """``with serve() as url:`` runs the dashboard for a browser test, over whatever records the caller patched in."""
    return _serve_app


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip("chromium is not installed: %s" % str(exc).splitlines()[0])
        yield chromium
        chromium.close()
