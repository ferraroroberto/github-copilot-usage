"""A stale tab that becomes visible again reloads once, and a slow load never paints over a newer one.

Runs at a phone viewport over synthetic records. Needs Playwright and an installed Chromium; without
them the module is skipped. Time is skewed through ``Date.now`` and the visibility event is
dispatched by hand, since a headless page is always visible and cannot wait out a poll interval.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from tests.e2e.test_dashboard_controls_ui import base_url  # noqa: E402,F401  (the shared synthetic-records server fixture)

SKEW_JS = """
window.__skew = 0;
const realNow = Date.now.bind(Date);
Date.now = () => realNow() + window.__skew;
"""
SHOW = "document.dispatchEvent(new Event('visibilitychange'))"
SENTINEL = 99999


@pytest.fixture
def page(phone_page, base_url):
    """A loaded dashboard whose summary requests are counted and, on demand, held back."""
    phone_page.add_init_script(SKEW_JS)
    phone_page.summary_calls = []  # type: ignore[attr-defined]
    phone_page.held = []  # type: ignore[attr-defined]
    phone_page.hold_next = False  # type: ignore[attr-defined]

    def on_summary(route) -> None:
        phone_page.summary_calls.append(route.request.url)
        if phone_page.hold_next:
            phone_page.hold_next = False
            phone_page.held.append(route)
        else:
            route.continue_()

    phone_page.route("**/api/summary*", on_summary)
    phone_page.goto(base_url + "/")
    phone_page.wait_for_selector("#session-list li", state="attached")
    phone_page.wait_for_function("document.getElementById('freshness').textContent.length > 0")
    return phone_page


def _skew(page, ms: int) -> None:
    page.evaluate("ms => { window.__skew = ms; }", ms)


def _settle(page) -> None:
    page.wait_for_timeout(400)


def test_returning_loads_only_once_the_tab_is_stale_and_then_exactly_once(page):
    before = len(page.summary_calls)
    _skew(page, 5_000)  # back before a poll interval has passed: nothing to load
    page.evaluate(SHOW)
    _settle(page)
    assert len(page.summary_calls) == before

    _skew(page, 31_000)
    page.evaluate(SHOW)
    _settle(page)
    assert len(page.summary_calls) == before + 1
    page.evaluate(SHOW)  # the figures are fresh again: a second return loads nothing
    _settle(page)
    assert len(page.summary_calls) == before + 1


def test_a_load_already_running_is_not_doubled_and_never_paints_over_a_newer_one(page):
    before = len(page.summary_calls)
    page.hold_next = True
    _skew(page, 31_000)
    page.evaluate(SHOW)
    for _ in range(50):
        if page.held:
            break
        page.wait_for_timeout(100)
    assert len(page.held) == 1
    page.evaluate(SHOW)  # a load is already in flight, so no second one starts
    _settle(page)
    assert len(page.summary_calls) == before + 1

    page.click("#menu-btn")  # a newer load through the refresh menu draws first
    page.click('[data-track="refresh"]')
    for _ in range(50):
        if len(page.summary_calls) == before + 2:
            break
        page.wait_for_timeout(100)
    _settle(page)
    drawn = page.locator("#t-credits").text_content()

    held = page.held[0]
    response = held.fetch()
    body = response.json()
    body["totals"]["credits"] = SENTINEL
    held.fulfill(response=response, json=body)
    _settle(page)
    assert page.locator("#t-credits").text_content() == drawn
    assert str(SENTINEL) not in page.locator("#t-credits").text_content().replace(",", "")
