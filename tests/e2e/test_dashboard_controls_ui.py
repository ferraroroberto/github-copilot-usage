"""Session names, the telemetry start date, collapsible cards and chart history in a real browser.

Runs at a phone viewport (the tab test also checks a desktop one) over synthetic records and a throwaway config file.
Needs Playwright and an installed Chromium; without them the module is skipped with that reason.
The page's usage-log posts are intercepted so the run writes nothing to the user's data.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from src import aggregate, config, refresh  # noqa: E402
from src.vscode_parser import RequestRecord  # noqa: E402

NOW = datetime.now(tz=timezone.utc)
DESKTOP = {"width": 1280, "height": 900}


def _records() -> List[RequestRecord]:
    rows: List[RequestRecord] = []
    for n, days_ago in enumerate([0, 0, 1, 3, 20, 50]):
        rows.append(RequestRecord(
            ide="Code", source="vscode", project_key="e:\\alpha", project_name="alpha", project_path=None,
            session_id="sess-recent" if days_ago < 10 else "sess-old",
            session_title="Recent session" if days_ago < 10 else "Old session",
            request_id="req-%02d" % n, ts=NOW - timedelta(days=days_ago, minutes=n),
            message="synthetic prompt %02d" % n, mode="agent", model_requested="m", model="gpt-5-mini",
            prompt_tokens=1000 + n, completion_tokens=100, credits=1.0, elapsed_ms=500,
        ))
    return rows


@pytest.fixture(scope="module")
def base_url(serve, tmp_path_factory) -> Iterator[str]:
    original = (aggregate.gather_records, config.CONFIG_PATH, refresh.store)
    aggregate.gather_records = lambda: (_records(), [])  # type: ignore[assignment]
    config.CONFIG_PATH = Path(tmp_path_factory.mktemp("cfg")) / "config.json"
    config._loaded, config._cache = False, {}
    refresh.store = refresh.SnapshotStore()
    with serve() as url:
        yield url
    aggregate.gather_records, config.CONFIG_PATH, refresh.store = original  # type: ignore[assignment]
    config._loaded, config._cache = False, {}


@pytest.fixture
def page(phone_page, base_url):
    phone_page.goto(base_url + "/")
    phone_page.wait_for_selector("#session-list li", state="attached")
    yield phone_page
    config.save({"telemetry_start_date": "", "session_names": {}})


def _open_tab(page, name: str) -> None:
    """Open one tab of the bottom-tab nav; the chosen tab is remembered across a reload."""
    page.click(f'.tabs .tab[data-tab="{name}"]')
    page.wait_for_selector(f"#pane-{name}:not([hidden])")


def _no_horizontal_scroll(page) -> bool:
    return page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_a_session_can_be_renamed_and_restored(page):
    _open_tab(page, "sessions")
    page.click("#session-list li")
    page.wait_for_selector("#session-dialog[open]")
    assert page.locator("#sd-name-clear").is_hidden()
    assert page.locator("#sd-save").is_disabled()  # the sheet's one primary wakes only once the name changes
    page.fill("#sd-name", "Billing investigation")
    assert page.locator("#sd-save").is_enabled()
    page.click("#sd-save")
    page.wait_for_function("document.querySelector('#sd-title').textContent === 'Billing investigation'")
    assert page.locator("#sd-name-clear").is_visible()
    page.click("#sd-close")

    page.reload()
    page.wait_for_selector("#session-list li")
    assert "Billing investigation" in page.locator("#session-list").inner_text()

    page.locator("#session-list li", has_text="Billing investigation").click()
    page.wait_for_selector("#session-dialog[open]")
    page.click("#sd-name-clear")
    page.wait_for_function("document.querySelector('#sd-title').textContent === 'Recent session'")
    page.click("#sd-close")
    assert "Billing investigation" not in page.locator("#session-list").inner_text()


def test_the_start_date_leaves_earlier_records_out_and_labels_the_coverage(page):
    page.click("#period-seg [data-period=all]")
    page.wait_for_function("document.querySelector('#t-requests').textContent === '6'")
    assert page.locator("#coverage-note").is_hidden()

    start = (date.today() - timedelta(days=10)).isoformat()
    page.click("#settings-btn")
    page.fill("#set-start", start)
    page.wait_for_function("document.querySelector('#coverage-note') && !document.querySelector('#coverage-note').hidden")
    assert "Partial coverage" in page.locator("#coverage-note").inner_text()
    page.click("#settings-close")
    page.wait_for_function("document.querySelector('#t-requests').textContent === '4'")

    page.click("#period-seg [data-period=today]")
    page.wait_for_function("document.querySelector('#t-requests').textContent === '2'")
    assert page.locator("#coverage-note").is_hidden()  # today is wholly after the start date

    page.click("#settings-btn")
    page.click("#set-start-clear")
    page.click("#settings-close")
    page.click("#period-seg [data-period=all]")
    page.wait_for_function("document.querySelector('#t-requests').textContent === '6'")
    assert _no_horizontal_scroll(page)


def test_a_card_collapses_and_stays_collapsed_after_a_reload(page):
    _open_tab(page, "overview")
    card = page.locator("details.card--collapsible", has=page.locator("h3.collapse-title", has_text="Data sources"))
    summary = card.locator(":scope > summary")
    body = card.locator(".source-list")
    assert card.get_attribute("open") is not None and body.is_visible()
    assert summary.bounding_box()["height"] == 52  # the vendored disclosure's closed height
    summary.click()
    assert body.is_hidden() and card.get_attribute("open") is None
    assert summary.bounding_box()["height"] == 52  # folding never moves the row
    assert card.locator("h3.collapse-title").is_visible()  # the heading stays to unfold it by

    page.reload()
    page.wait_for_selector("#session-list li", state="attached")
    card = page.locator("details.card--collapsible", has=page.locator("h3.collapse-title", has_text="Data sources"))
    assert card.locator(".source-list").is_hidden()
    card.locator(":scope > summary").click()
    assert card.locator(".source-list").is_visible()
    assert card.locator(":scope > summary").bounding_box()["height"] >= 44


def test_the_trend_steps_through_older_windows_and_a_bucket_tap_focuses_the_dashboard(page):
    _open_tab(page, "trends")
    assert page.locator("#charts-card .chart-wrap").first.is_visible()
    assert page.locator("#charts-card .collapse-title").count() == 0  # no card that only repeats the tab's name
    # Today draws 14 days; the records 20 and 50 days old sit in older windows.
    page.wait_for_function("document.querySelector('#trend-range').textContent !== ''")
    latest_range = page.locator("#trend-range").inner_text()
    assert page.locator("#trend-newer").is_disabled() and page.locator("#trend-latest").is_disabled()

    page.click("#trend-older")
    page.wait_for_function("document.querySelector('#trend-latest').disabled === false")
    assert page.locator("#trend-range").inner_text() != latest_range
    assert page.locator("#trend-newer").is_enabled()
    # The tokens-per-credit and cache charts are drawn from the same window as the credits chart.
    older_labels = page.evaluate("Chart.getChart('chart-credits').data.labels")
    for chart in ("chart-efficiency", "chart-cache"):
        assert page.locator("#" + chart).is_visible()
        assert page.evaluate("id => Chart.getChart(id).data.labels", chart) == older_labels
    page.click("#trend-latest")
    page.wait_for_function("document.querySelector('#trend-latest').disabled === true")
    assert page.locator("#trend-range").inner_text() == latest_range

    # Tap the newest bar: the whole dashboard narrows to that bucket's days and Back undoes it.
    x, y = page.evaluate(
        "(() => { const c = Chart.getChart('chart-credits'); const r = c.canvas.getBoundingClientRect();"
        " return [r.left + c.chartArea.right - 6, r.top + (c.chartArea.top + c.chartArea.bottom) / 2]; })()"
    )
    page.mouse.click(x, y)
    page.wait_for_selector("#focus-bar:not([hidden])")
    assert page.locator('#period-seg [aria-pressed="true"]').count() == 0
    assert "Focused on" in page.locator("#focus-label").inner_text()
    assert page.locator("#trend-nav").is_hidden()
    page.wait_for_function("document.querySelector('#t-requests').textContent !== '—'")

    page.click("#focus-clear")
    page.wait_for_selector("#focus-bar", state="hidden")
    assert page.locator('#period-seg [aria-pressed="true"]').inner_text() == "Today"
    assert page.locator("#trend-nav").is_visible()
    assert _no_horizontal_scroll(page)


def test_each_tab_shows_its_own_pane_under_one_page_header(page):
    tabs = page.eval_on_selector_all(".tabs .tab", "ts => ts.map(t => [t.dataset.tab, t.textContent.trim()])")
    assert [name for name, _ in tabs] == ["overview", "trends", "breakdown", "sessions"]
    for name, label in tabs:
        _open_tab(page, name)
        shown = page.eval_on_selector_all(".pane", "ps => ps.filter(p => !p.hidden).map(p => p.id)")
        assert shown == ["pane-" + name]
        assert page.inner_text("#head-title") == label  # the header names the open tab
        assert page.locator("#theme-toggle").is_visible() and page.locator("#settings-btn").is_visible()
        assert _no_horizontal_scroll(page)
        assert page.locator("#filters-btn").is_visible() == (name == "sessions")  # Filters & sort belongs to Sessions only

    # Sessions: the sort and a second copy of the period sit in one sheet; the trigger counts what differs from the defaults.
    assert page.get_attribute("#filters-btn", "aria-label") == "Filters & sort" and page.locator("#filters-count").is_hidden()
    page.click("#filters-btn")
    page.wait_for_selector("#filters-dialog[open]")
    page.click('#filters-dialog [data-sort="recent"]')
    page.click('#filters-dialog [data-period="all"]')
    page.wait_for_function("document.querySelectorAll('#session-list li').length === 2")
    assert page.inner_text("#filters-count") == "2"
    assert page.get_attribute("#filters-btn", "aria-label") == "Filters & sort, 2 active: period All time, sort Recent"
    assert page.get_attribute('#period-seg [data-period="all"]', "aria-pressed") == "true"  # the page's control follows the sheet
    assert page.evaluate("document.querySelector('#filters-done').getBoundingClientRect().bottom <= window.innerHeight")
    page.click("#filters-clear")
    page.wait_for_function("document.querySelector('#filters-count').hidden")
    assert page.get_attribute('#period-seg [data-period="today"]', "aria-pressed") == "true"
    page.click("#filters-done")
    page.wait_for_selector("#filters-dialog", state="hidden")

    page.click("#settings-btn")
    page.wait_for_selector("#settings-dialog[open]")  # Settings is the header gear, never a tab
    page.click("#settings-close")

    page.set_viewport_size(DESKTOP)  # the same panes and no sideways scroll on a wide window
    for name, _ in tabs:
        _open_tab(page, name)
        assert page.eval_on_selector_all(".pane", "ps => ps.filter(p => !p.hidden).map(p => p.id)") == ["pane-" + name]
        assert _no_horizontal_scroll(page)


def test_the_refresh_menu_reports_through_a_toast_that_paints_above_an_open_sheet(page):
    items = '[role="menu"] [role="menuitem"]'
    page.click("#menu-btn")
    page.wait_for_selector(items)
    assert page.eval_on_selector_all(items, "els => els.map(e => e.textContent.trim())") == ["Refresh", "Force refresh"]
    page.keyboard.press("Escape")
    page.wait_for_selector('[role="menu"]', state="detached")

    page.click("#menu-btn")
    page.click('[role="menuitem"]:has-text("Force refresh")')
    page.wait_for_function("document.querySelector('#toast') && !document.querySelector('#toast').hidden")
    assert page.inner_text("#toast").startswith("Rebuilt from the files")

    # The toast also paints above an open sheet.
    page.click("#settings-btn")
    page.wait_for_selector("#settings-dialog[open]")
    page.click('#set-textsize [data-textsize="large"]')
    page.wait_for_function(
        "document.querySelector('#toast') && !document.querySelector('#toast').hidden"
        " && !document.querySelector('#toast').textContent.startsWith('Rebuilt')"
    )
    # A modal dialog makes the rest of the page inert, so hit-testing cannot see the toast; its
    # place in the top layer (opened after the dialog, so stacked above it) is what carries it.
    assert page.evaluate("document.querySelector('#toast').matches(':popover-open')")
    box = page.locator("#toast").bounding_box()
    viewport = page.viewport_size
    assert 0 <= box["x"] and box["x"] + box["width"] <= viewport["width"]
    assert box["y"] + box["height"] <= viewport["height"]  # on screen, above the foot of the viewport
