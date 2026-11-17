"""Shared test fixtures: the synthetic-record factory (no real logs, prompts or figures), an isolated config file, and a live server plus headless Chromium for the browser tests.

The browser tests spend loopback ports sparingly: each test module shares one Chromium and one phone browser
context, so the connections to its test server are kept alive and reused instead of redialled per test. The
scope stays per module because a Playwright session left open keeps an event loop running for the async unit
tests that follow. A drained ephemeral port pool is reported as such instead of as a page failure."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import Callable, Iterator, List, Optional

import pytest
import uvicorn

from src import config
from src.vscode_parser import RequestRecord

# Noon local time: a day-bucketed period test cannot straddle midnight.
TODAY_NOON = datetime.combine(date.today(), clock(12, 0)).astimezone()

PHONE = {"width": 390, "height": 844}
KEEP_ALIVE_SECONDS = 120

# Chromium reports a drained ephemeral port pool as this error on a page load, whatever the page is.
PORT_POOL_ERROR = "ERR_ADDRESS_IN_USE"
PORT_POOL_MESSAGE = (
    "PORT POOL EXHAUSTED: the machine's loopback ephemeral ports are used up (net::ERR_ADDRESS_IN_USE), "
    "so this is the host's socket pool, not a defect in the dashboard. Let the TIME_WAIT sockets drain "
    "(a couple of minutes) and rerun; do not change the port range or TcpTimedWaitDelay."
)
_port_pool_failures: List[str] = []


def port_pool_failure(report_text: str) -> Optional[str]:
    """The distinct message when a failure's text names the drained port pool, else None."""
    return PORT_POOL_MESSAGE if PORT_POOL_ERROR in report_text else None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed and call.excinfo is not None and port_pool_failure(str(call.excinfo.value)):
        _port_pool_failures.append(item.nodeid)
        report.longrepr = "%s\n\n%s" % (PORT_POOL_MESSAGE, report.longrepr)


def pytest_terminal_summary(terminalreporter) -> None:
    if _port_pool_failures:
        terminalreporter.section("port pool")
        terminalreporter.write_line(PORT_POOL_MESSAGE, red=True)
        for nodeid in _port_pool_failures:
            terminalreporter.write_line("  " + nodeid)


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
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        timeout_keep_alive=KEEP_ALIVE_SECONDS,  # the default 5 s drops a connection a slow test still means to reuse
    ))
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


@pytest.fixture(scope="module")
def phone_context(browser):
    """The one phone-sized browser context of a test module; its usage-log posts are answered locally and recorded in ``usage_posts``.

    A context owns its connection pool, so sharing it lets every test reuse the connections to a server
    instead of dialling new loopback sockets.
    """
    context = browser.new_context(viewport=PHONE, has_touch=True, is_mobile=True)
    context.usage_posts = []
    context.route(
        "**/api/usage/**",
        lambda route: (context.usage_posts.append(route.request.url), route.fulfill(status=204)),
    )
    yield context
    context.close()


@pytest.fixture
def phone_page(phone_context):
    """A new page in the shared context; storage, cookies and the recorded posts are reset around it."""
    phone_context.usage_posts.clear()
    page = phone_context.new_page()
    yield page
    try:
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
    except Exception:  # the page was closed or sits on a blank document with no storage
        pass
    page.close()
    phone_context.clear_cookies()
