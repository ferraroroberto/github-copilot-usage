"""The main page keeps the fleet's request budget in a real browser.

On a fake clock the page is loaded, left alone for a visible minute and then for a hidden minute,
and every API request is counted by path (the probe and its rules: `_request_budget.py`). The
records are synthetic. Needs Playwright with an installed Chromium and Python 3.11+ (the probe
reads `.fleet.toml` with `tomllib`); without them the module is skipped with that reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
pytest.importorskip("tomllib", reason="the request-budget probe reads .fleet.toml with tomllib (Python 3.11+)")

from tests.e2e.budget._request_budget import (  # noqa: E402
    assert_request_budget,
    measure_request_budget,
    visible_budget,
)
from tests.e2e.test_dashboard_controls_ui import base_url  # noqa: E402,F401  (the shared synthetic-records server fixture)

REPO_ROOT = Path(__file__).resolve().parents[3]

# The one request the hidden minute may hold: the usage log sends its `session_end` event as a beacon
# in the same instant the page turns hidden, so the visit's last event is not lost with the tab. It
# is a single send at the transition; nothing is sent for the rest of the minute.
HIDE_BEACON = "/api/usage/events"


def test_main_page_keeps_its_request_budget(phone_page, base_url):
    reading = measure_request_budget(phone_page, base_url + "/")
    assert reading.hidden.pop(HIDE_BEACON, 0) <= 1, f"the usage log beacons once on hiding: {reading.summary()}"
    assert_request_budget(reading, visible_budget(REPO_ROOT))
