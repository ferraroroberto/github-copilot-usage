"""Settings → Available features in a real browser, at a phone and a desktop width, in both themes.

Each block is switched off and on again through the page: its UI hides, its calls stop, and it
returns with no reload. Needs Playwright and an installed Chromium; without them the module is skipped.
Every usage-log post is intercepted and counted, so the run writes nothing to the user's data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from src import aggregate, billing, config, refresh  # noqa: E402
from tests.e2e.test_dashboard_controls_ui import VIEWPORTS, _records  # noqa: E402


@pytest.fixture(scope="module")
def base_url(serve, tmp_path_factory) -> Iterator[str]:
    original = (aggregate.gather_records, config.CONFIG_PATH, refresh.store, billing._read_pat)
    aggregate.gather_records = lambda: (_records(), [])  # type: ignore[assignment]
    config.CONFIG_PATH = Path(tmp_path_factory.mktemp("cfg")) / "config.json"
    config._loaded, config._cache = False, {}
    refresh.store = refresh.SnapshotStore()
    billing._read_pat = lambda: ""  # type: ignore[assignment]
    with serve() as url:
        yield url
    aggregate.gather_records, config.CONFIG_PATH, refresh.store, billing._read_pat = original  # type: ignore[assignment]
    config._loaded, config._cache = False, {}


@pytest.fixture(params=sorted(VIEWPORTS))
def page(browser, base_url, request):
    config.save({"features": {}})
    phone = request.param == "phone"
    context = browser.new_context(viewport=VIEWPORTS[request.param], has_touch=phone, is_mobile=phone)
    pg = context.new_page()
    pg.usage_posts = []  # type: ignore[attr-defined]
    pg.billing_calls = []  # type: ignore[attr-defined]
    context.route("**/api/usage/**", lambda route: (pg.usage_posts.append(route.request.url), route.fulfill(status=204)))
    context.route("**/api/billing*", lambda route: (pg.billing_calls.append(route.request.url), route.continue_()))
    pg.goto(base_url + "/")
    pg.wait_for_selector("#session-list li", state="attached")
    yield pg
    context.close()
    config.save({"features": {}})


def _open_settings(page) -> None:
    page.click("#settings-btn")
    page.wait_for_selector("#settings-dialog[open]")


def _switch(page, block: str):
    return page.locator('[data-feature="%s"]' % block)


LABELS = {
    "copilot_cli": "Copilot CLI sessions", "billing_card": "Official billing card",
    "pace_alert": "Pace alert", "usage_log": "Usage log",
}


def _toggle(page, block: str, on: bool) -> None:
    """Flip one switch and wait for the server's answer to have been applied to the page (the toast says so)."""
    if _switch(page, block).get_attribute("aria-checked") == str(on).lower():
        return
    _switch(page, block).click()
    page.wait_for_function(
        "t => document.querySelector('#toast').textContent === t",
        arg=LABELS[block] + (" on" if on else " off"),
    )


def _flush_usage(page) -> int:
    """Leave the page the way a visitor does, which makes a running usage log send, and count the sends."""
    before = len(page.usage_posts)
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    page.wait_for_timeout(300)
    return len(page.usage_posts) - before


def test_every_block_is_on_by_default(page):
    _open_settings(page)
    assert [b.get_attribute("aria-checked") for b in page.locator("[data-feature]").all()] == ["true"] * 4
    assert page.locator("#billing-card").is_visible()


def test_billing_card_off_hides_it_and_stops_its_calls_then_restores_them(page):
    page.wait_for_function("document.querySelector('#billing-note').textContent !== ''")
    assert page.locator("#billing-card").is_visible() and page.billing_calls
    _open_settings(page)
    assert page.locator('.settings-row[data-feature-ui="billing_card"]').is_visible()

    _toggle(page, "billing_card", False)
    assert page.locator("#billing-card").is_hidden()
    assert page.locator('.settings-row[data-feature-ui="billing_card"]').is_hidden()
    calls = len(page.billing_calls)
    page.click("#settings-close")
    page.click('[data-period="week"]')  # a refresh, which is what polls the billing card
    page.wait_for_timeout(500)
    assert len(page.billing_calls) == calls

    _open_settings(page)
    _toggle(page, "billing_card", True)
    page.click("#settings-close")
    page.wait_for_function("document.querySelector('#billing-card').offsetParent !== null")
    assert len(page.billing_calls) > calls


def test_pace_alert_off_hides_the_alerts_group_and_on_restores_it(page):
    _open_settings(page)
    assert page.locator("#group-alerts").is_visible()
    _toggle(page, "pace_alert", False)
    assert page.locator("#group-alerts").is_hidden() and page.locator("#set-alert-note").is_hidden()
    _toggle(page, "pace_alert", True)
    assert page.locator("#group-alerts").is_visible()


def test_usage_log_off_sends_nothing_and_on_sends_again(page):
    assert _flush_usage(page) > 0  # the log is running
    _open_settings(page)
    _toggle(page, "usage_log", False)
    page.click("#settings-close")
    page.click("#tab-trends")
    assert _flush_usage(page) == 0

    _open_settings(page)
    _toggle(page, "usage_log", True)
    page.click("#settings-close")
    assert _flush_usage(page) > 0


def test_copilot_cli_switch_persists_across_a_reload(page, base_url):
    _open_settings(page)
    _toggle(page, "copilot_cli", False)
    page.reload()
    page.wait_for_selector("#session-list li", state="attached")
    _open_settings(page)
    assert _switch(page, "copilot_cli").get_attribute("aria-checked") == "false"
    assert config.load()["features"] == {"copilot_cli": False}


def test_a_block_that_cannot_run_here_says_so_and_stays_switchable(page):
    _open_settings(page)
    hint = _switch(page, "billing_card").locator("xpath=ancestor::label").locator(".settings-row-hint")
    assert hint.inner_text() == "Unavailable on this machine"
    note = page.locator("#features-note")
    assert note.is_visible() and "GITHUB_COPILOT_BILLING_PAT" in note.inner_text()
    _toggle(page, "billing_card", False)
    assert page.locator("#billing-card").is_hidden()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_features_group_fits_the_screen_in_both_themes(page, theme):
    page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
    _open_settings(page)
    page.locator("#group-features").scroll_into_view_if_needed()
    overflow: List[bool] = page.evaluate(
        "() => [document.documentElement, document.querySelector('#settings-dialog')]"
        ".map(el => el.scrollWidth > el.clientWidth)"
    )
    assert overflow == [False, False]
    for switch in page.locator("[data-feature]").all():
        box = switch.bounding_box()
        row = switch.locator("xpath=ancestor::label").bounding_box()
        assert box["x"] + box["width"] <= row["x"] + row["width"] + 1
        assert row["height"] >= 44
