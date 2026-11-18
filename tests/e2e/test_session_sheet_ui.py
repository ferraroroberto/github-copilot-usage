"""The session sheet's request rows in a real browser at a phone viewport, over synthetic records.

Needs Playwright and an installed Chromium; without them the module is skipped with that reason.
A tap opens a request in place, so every token bucket and the stored prompt are readable
without a hover.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterator, List

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from src import aggregate  # noqa: E402
from src.vscode_parser import RequestRecord  # noqa: E402
from tests.conftest import TODAY_NOON  # noqa: E402

LONG_PROMPT = "explain this module " + "and its callers " * 20


def _record(n: int, **over) -> RequestRecord:
    fields = dict(
        ide="Code", source="vscode", project_key="e:\\alpha", project_name="alpha", project_path=None,
        session_id="sess-a", session_title="Growing session", request_id="req-%02d" % n,
        ts=TODAY_NOON - timedelta(minutes=30 - n), message="prompt %02d" % n, mode="agent",
        model_requested="auto", model="gpt-5-mini", prompt_tokens=10_000 + 2_000 * n, completion_tokens=300,
        credits=1.0, elapsed_ms=1500, cache_read_tokens=4_000, cache_write_tokens=500,
        usage_source="vscode",
    )
    fields.update(over)
    return RequestRecord(**fields)


def _records() -> List[RequestRecord]:
    rows = [_record(n) for n in range(4)]
    rows[0].message = LONG_PROMPT
    rows.append(_record(4, source="cli", ide="Copilot CLI", usage_source="events.jsonl-allocated",
                        usage_estimated=True, message="estimated cli prompt"))
    rows.append(_record(5, source="cli", ide="Copilot CLI", usage_source="unavailable", prompt_tokens=None,
                        completion_tokens=None, cache_read_tokens=None, cache_write_tokens=None,
                        message="unknown cli prompt"))
    return rows


@pytest.fixture(scope="module")
def base_url(serve) -> Iterator[str]:
    original = aggregate.gather_records
    aggregate.gather_records = lambda: (_records(), [])  # type: ignore[assignment]
    with serve() as url:
        yield url
    aggregate.gather_records = original  # type: ignore[assignment]


@pytest.fixture()
def page(phone_page):
    return phone_page


def _open_sheet(page, base_url: str) -> None:
    page.goto(base_url + "/")
    page.click('.tabs .tab[data-tab="sessions"]')
    page.click("#session-list button[data-sid]")
    page.wait_for_selector("#sd-requests li.action-row")


def _facts(page, index: int) -> dict:
    block = page.locator("#sd-req-%d" % index)
    return {
        label.strip(): value.strip()
        for label, value in zip(block.locator("dt").all_inner_texts(), block.locator("dd").all_inner_texts())
    }


def test_requests_open_in_place_with_every_bucket_and_the_sheet_lists_prompt_growth(page, base_url):
    _open_sheet(page, base_url)

    # a tap opens a measured request: every bucket and the full prompt, no hover
    assert page.locator("#sd-req-0").is_hidden()
    page.locator("#sd-requests button[data-req='0']").click()
    assert page.locator("#sd-requests button[data-req='0']").get_attribute("aria-expanded") == "true"
    facts = _facts(page, 0)
    assert facts["Model requested"] == "auto"
    assert facts["Input volume"] == "10,000"
    assert facts["Context processed"] == "14,000"
    assert facts["Uncached input"] == "9,500"
    assert facts["Cache read"] == "4,000" and facts["Cache write"] == "500"
    assert facts["Output"] == "300"
    assert page.locator("#sd-req-0 .req-prompt").inner_text() == LONG_PROMPT
    assert "first 200 characters" in page.locator("#sd-req-0").inner_text()
    page.locator("#sd-requests button[data-req='0']").click()
    assert page.locator("#sd-req-0").is_hidden()

    # an estimate carries ~ and its source; an unrecorded figure is a dash
    page.locator("#sd-requests button[data-req='4']").click()
    assert _facts(page, 4)["Input volume"].startswith("~")
    assert "events.jsonl-allocated" in page.locator("#sd-req-4").inner_text()
    page.locator("#sd-requests button[data-req='5']").click()
    unknown = _facts(page, 5)
    assert unknown["Input volume"] == "—" and unknown["Output"] == "—" and unknown["Cache read"] == "—"
    assert "recorded no token counts" in page.locator("#sd-req-5").inner_text()

    # four measured requests (the estimated and the unknown one are left out): four sizes, three changes
    page.click("#sd-diagnostics .growth-box > summary")
    assert page.locator("#sd-diagnostics .growth-list li").count() == 4
    changes = page.locator("#sd-diagnostics .growth-change").all_inner_texts()
    assert changes[0] == "first" and changes[1] == "+20%"
    assert sum(1 for c in changes if c != "first") == 3


def test_the_sheet_is_as_wide_as_the_page_column_and_the_prompt_opens_first(page, base_url):
    _open_sheet(page, base_url)
    row = page.locator("#sd-requests button[data-req='0']")

    # the row says it opens, and the prompt is the first thing it shows, above every figure
    row.click()
    assert row.get_attribute("aria-expanded") == "true"
    prompt = page.locator("#sd-req-0 .req-prompt").bounding_box()
    figures = page.locator("#sd-req-0 .req-facts").bounding_box()
    assert prompt["y"] < figures["y"]

    # the wide sheet lines up with the cards behind it at a phone and a tablet width
    for width, height in ((390, 844), (800, 900)):
        page.set_viewport_size({"width": width, "height": height})
        page.locator("#sd-close").click()
        page.wait_for_selector("#session-dialog", state="hidden")
        card = page.locator("#pane-sessions .card").first.bounding_box()
        page.click("#session-list button[data-sid]")
        page.wait_for_selector("#session-dialog", state="visible")
        sheet = page.locator("#session-dialog").bounding_box()
        assert abs(sheet["width"] - card["width"]) <= 1, width
        assert abs(sheet["x"] - card["x"]) <= 1, width


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_on_a_desktop_the_sheet_shares_the_page_columns_edges_beside_the_side_nav(browser, base_url, scheme):
    # a fine pointer is what gives the wide layout its side nav, so this needs a desktop context, not the phone one
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
    context.route("**/api/usage/**", lambda route: route.fulfill(status=204))
    page = context.new_page()
    try:
        _open_sheet(page, base_url)
        for width in (1440, 1100, 1024):
            page.set_viewport_size({"width": width, "height": 900})
            page.locator("#sd-close").click()
            page.wait_for_selector("#session-dialog", state="hidden")
            card = page.locator("#pane-sessions .card").first.bounding_box()
            page.click("#session-list button[data-sid]")
            page.wait_for_selector("#session-dialog", state="visible")
            sheet = page.locator("#session-dialog").bounding_box()
            assert abs(sheet["x"] - card["x"]) <= 1, width
            assert abs(sheet["x"] + sheet["width"] - card["x"] - card["width"]) <= 1, width
    finally:
        context.close()
