"""The request explorer in a real browser at a phone viewport, over synthetic records.

Needs Playwright and an installed Chromium (``pip install playwright`` then
``playwright install chromium``); without them the module is skipped with that reason.
The page's usage-log posts are intercepted so the run writes nothing to the user's data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator, List

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from src import aggregate  # noqa: E402
from src.vscode_parser import RequestRecord  # noqa: E402

NOW = datetime.now(tz=timezone.utc)
PHONE = {"width": 390, "height": 844}


def _records() -> List[RequestRecord]:
    rows: List[RequestRecord] = []
    for n in range(30):
        beta = n % 3 == 0
        rows.append(RequestRecord(
            ide="Code",
            source="vscode",
            project_key="e:\beta" if beta else "e:\alpha",
            project_name="beta" if beta else "alpha",
            project_path=None,
            session_id="sess-beta" if beta else "sess-alpha",
            session_title="Beta session" if beta else "Alpha session",
            request_id="req-%02d" % n,
            ts=NOW - timedelta(minutes=n),
            message="synthetic prompt %02d" % n,
            mode="agent" if n % 2 else "ask",
            model_requested="m",
            model="claude-sonnet" if n % 4 == 0 else "gpt-5-mini",
            prompt_tokens=1000 + n,
            completion_tokens=100,
            credits=None if n % 10 == 5 else float(n % 7),
            elapsed_ms=500 + n * 10,
            error=n == 7,
        ))
    return rows


@pytest.fixture(scope="module")
def base_url(serve) -> Iterator[str]:
    original = aggregate.gather_records
    aggregate.gather_records = lambda: (_records(), [])  # type: ignore[assignment]
    with serve() as url:
        yield url
    aggregate.gather_records = original  # type: ignore[assignment]


@pytest.fixture()
def page(browser):
    context = browser.new_context(viewport=PHONE, has_touch=True, is_mobile=True)
    context.route("**/api/usage/**", lambda route: route.fulfill(status=204))
    pg = context.new_page()
    yield pg
    context.close()


def _rows(page) -> int:
    return page.locator("#rq-list li").count()


def _open(page, base_url: str, query: str = "") -> None:
    page.goto(base_url + "/" + query)
    if not query:
        page.click('.tabs .tab[data-tab="sessions"]')
        page.click("#requests-open")
    page.wait_for_selector("#rq-list li")


def test_filters_sort_and_url_state_survive_a_reload(page, base_url):
    _open(page, base_url)
    assert "view=requests" in page.url
    assert _rows(page) == 25  # first page of 30

    assert page.locator("#rq-filter-box[open]").count() == 0  # folded on a phone
    page.click("#rq-filter-box > summary")
    page.select_option("#rq-model", "claude-sonnet")
    page.wait_for_function("document.querySelectorAll('#rq-list li').length === 8")
    assert "model=claude-sonnet" in page.url

    page.fill("#rq-min", "3")
    page.wait_for_function("location.search.includes('min=3')")
    # claude-sonnet rows are n % 4 == 0; credits are n % 7, so a minimum of 3 drops some of the 8
    page.wait_for_function("document.querySelectorAll('#rq-list li').length < 8")
    assert page.locator("#rq-list li .sess-credits", has_text="unbilled").count() == 0
    assert page.locator("#rq-list li").count() == 4

    page.select_option("#rq-sort", "credits")
    page.wait_for_function("document.querySelector('#rq-order').textContent === 'Most credits first'")
    first = page.locator("#rq-list li .sess-credits").first.inner_text()
    last = page.locator("#rq-list li .sess-credits").last.inner_text()
    assert float(first.split()[0]) >= float(last.split()[0])

    shown, url = page.locator("#rq-list").inner_text(), page.url
    page.reload()
    page.wait_for_selector("#rq-list li")
    assert page.url == url
    assert page.input_value("#rq-model") == "claude-sonnet"
    assert page.input_value("#rq-min") == "3"
    assert page.input_value("#rq-sort") == "credits"
    assert page.locator("#rq-list").inner_text() == shown


def test_pagination_walks_the_pages(page, base_url):
    _open(page, base_url)
    assert page.inner_text("#rq-page") == "Page 1 of 2"
    assert page.is_disabled("#rq-prev")
    page.click("#rq-next")
    page.wait_for_function("document.querySelectorAll('#rq-list li').length === 5")
    assert page.inner_text("#rq-page") == "Page 2 of 2"
    assert "page=2" in page.url
    assert page.is_disabled("#rq-next")


def test_unbilled_rows_show_without_a_credit_range_and_a_row_opens_its_session(page, base_url):
    _open(page, base_url, "?view=requests&period=week&q=prompt 05")
    page.wait_for_function("document.querySelectorAll('#rq-list li').length === 1")
    assert "unbilled" in page.locator("#rq-list li").first.inner_text()

    page.fill("#rq-q", "")
    page.wait_for_function("document.querySelectorAll('#rq-list li').length > 1")
    page.locator("#rq-list li").first.click()
    page.wait_for_selector("#session-dialog[open]")
    assert page.inner_text("#sd-title") in ("Alpha session", "Beta session")


def test_phone_layout_does_not_overflow_and_empty_state_reads(page, base_url):
    _open(page, base_url)
    for target in ("document.documentElement", "document.querySelector('#requests-dialog')"):
        assert page.evaluate("t => t.scrollWidth <= t.clientWidth + 1", page.evaluate_handle(target))
    page.fill("#rq-q", "nothing matches this text")
    page.wait_for_selector("#rq-empty:not([hidden])")
    assert page.inner_text("#rq-summary") == "No requests match"
    page.click("#rq-close")
    page.wait_for_function("!location.search.includes('view=requests')")
