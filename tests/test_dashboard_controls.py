"""Custom session names, the telemetry start date, chart history and focused ranges, on synthetic records."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

from src import aggregate, config, refresh

TODAY = date.today()


def _day(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def served(monkeypatch, settings_file, rec):
    """A client over three synthetic requests (40, 2 and 0 days old) and an isolated config file."""
    records = [
        rec(0, days_ago=40, session_id="old", session_title="Old chat", credits=3.0),
        rec(1, days_ago=2, session_id="new", session_title="New chat", credits=2.0),
        rec(2, days_ago=0, session_id="new", session_title="New chat", credits=1.0),
    ]
    monkeypatch.setattr(aggregate, "gather_records", lambda: (list(records), [{"ide": "Code"}]))
    monkeypatch.setattr(refresh, "store", refresh.SnapshotStore())
    from app.server import app

    return TestClient(app)


def _restart(monkeypatch) -> None:
    """Forget the in-memory config, as a new process would, so the next read comes from the file."""
    monkeypatch.setattr(config, "_loaded", False)
    monkeypatch.setattr(config, "_cache", {})


def _titles(client) -> dict:
    rows = client.get("/api/sessions", params={"period": "all"}).json()["sessions"]
    return {row["session_id"]: row["title"] for row in rows}


# ---- session names ----


def test_a_custom_name_replaces_the_title_everywhere_and_survives_a_restart(served, monkeypatch, settings_file):
    saved = served.put("/api/sessions/new/name", json={"name": "  Billing   fix "})
    assert saved.json() == {"session_id": "new", "name": "Billing fix"}

    _restart(monkeypatch)
    assert _titles(served) == {"old": "Old chat", "new": "Billing fix"}
    detail = served.get("/api/sessions/new").json()
    assert detail["title"] == "Billing fix" and detail["parsed_title"] == "New chat"
    assert detail["custom_name"] == "Billing fix"
    assert "Billing fix" in served.get("/api/export.csv", params={"period": "all"}).text
    found = served.get("/api/requests", params={"period": "all", "q": "billing fix"}).json()
    assert found["total"] == 2
    assert json.loads(settings_file.read_text(encoding="utf-8"))["session_names"] == {"new": "Billing fix"}


@pytest.mark.parametrize("cleared", ["", "   ", None])
def test_clearing_a_name_restores_the_parsed_title(served, monkeypatch, cleared):
    served.put("/api/sessions/new/name", json={"name": "Renamed"})
    assert served.put("/api/sessions/new/name", json={"name": cleared}).json()["name"] is None

    _restart(monkeypatch)
    assert _titles(served)["new"] == "New chat"
    detail = served.get("/api/sessions/new").json()
    assert detail["custom_name"] is None and detail["parsed_title"] == "New chat"


def test_a_name_the_server_refuses_changes_nothing(served):
    too_long = served.put("/api/sessions/new/name", json={"name": "x" * (config.MAX_SESSION_NAME + 1)})
    assert too_long.status_code == 400 and "at most" in too_long.json()["detail"]
    assert served.put("/api/sessions/new/name", json={"name": 7}).status_code == 400
    assert served.put("/api/sessions/nope/name", json={"name": "Ghost"}).status_code == 404
    assert _titles(served)["new"] == "New chat"
    assert served.get("/api/config").json()["session_names"] == {}


def test_the_name_of_a_session_that_is_gone_can_still_be_cleared(served):
    served.post("/api/config", json={"session_names": {"vanished": "Stale"}})
    assert served.put("/api/sessions/vanished/name", json={"name": ""}).status_code == 200
    assert served.get("/api/config").json()["session_names"] == {}


# ---- telemetry start date ----


def _start_at(client, days_ago: int) -> None:
    assert client.post("/api/config", json={"telemetry_start_date": _day(days_ago)}).status_code == 200


def test_records_before_the_start_date_are_in_no_total(served):
    everything = served.get("/api/summary", params={"period": "all"}).json()
    assert everything["totals"]["credits"] == 6.0 and everything["coverage"]["state"] == "full"

    _start_at(served, 10)
    counted = served.get("/api/summary", params={"period": "all"}).json()
    assert counted["totals"]["credits"] == 3.0 and counted["totals"]["requests"] == 2
    assert [m for m in counted["by_model"]] and sum(m["credits"] for m in counted["by_model"]) == 3.0
    assert counted["budget"]["used_credits"] == 3.0
    assert set(_titles(served)) == {"new"}
    assert served.get("/api/requests", params={"period": "all"}).json()["total"] == 2
    assert served.get("/api/export.csv", params={"period": "all"}).text.count("\n") == 3  # header + 2
    assert served.get("/api/sessions/old").json().get("error") == "session not found"


def test_a_period_the_start_date_cuts_into_reports_partial_coverage(served):
    _start_at(served, 10)
    coverage = {
        p: served.get("/api/summary", params={"period": p}).json()["coverage"]["state"]
        for p in ("today", "week", "month", "all")
    }
    assert coverage == {"today": "full", "week": "full", "month": "partial", "all": "partial"}
    assert served.get("/api/summary", params={"period": "all"}).json()["coverage"]["start_date"] == _day(10)


def test_a_period_before_the_start_date_is_none_not_zero(served):
    assert served.post("/api/config", json={"telemetry_start_date": (TODAY + timedelta(days=1)).isoformat()}).status_code == 200
    body = served.get("/api/summary", params={"period": "week"}).json()
    assert body["coverage"]["state"] == "none"
    assert served.get("/api/summary", params={"period": f"range:{_day(5)}..{_day(3)}"}).json()["coverage"]["state"] == "none"


def test_a_comparison_the_start_date_cuts_into_is_left_out(served):
    assert served.get("/api/summary", params={"period": "week"}).json()["prev_totals"] is not None
    _start_at(served, 10)
    assert served.get("/api/summary", params={"period": "week"}).json()["prev_totals"] is None
    assert served.get("/api/summary", params={"period": "today"}).json()["prev_totals"] is not None


def test_trend_buckets_say_how_much_of_them_the_start_date_covers(served):
    _start_at(served, 5)
    series = served.get("/api/trends", params={"period": "today"}).json()["time_series"]
    by_date = {b["date"]: b["coverage"] for b in series}
    assert by_date[_day(6)] == "none" and by_date[_day(5)] == "full" and by_date[_day(0)] == "full"
    months = served.get("/api/trends", params={"period": "all"}).json()["time_series"]
    assert months[-1]["coverage"] in ("partial", "full") and months[0]["coverage"] == "none"


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-40", "11/16/2026", 5])
def test_a_malformed_start_date_is_refused(served, bad):
    refused = served.post("/api/config", json={"telemetry_start_date": bad})
    assert refused.status_code == 400 and "telemetry_start_date" in refused.json()["detail"]
    assert served.get("/api/config").json()["telemetry_start_date"] == ""


def test_an_empty_start_date_counts_everything_again(served):
    _start_at(served, 10)
    served.post("/api/config", json={"telemetry_start_date": ""})
    assert served.get("/api/summary", params={"period": "all"}).json()["totals"]["credits"] == 6.0


def test_a_bad_value_written_into_config_json_is_ignored(served, monkeypatch, settings_file):
    settings_file.write_text(json.dumps({"telemetry_start_date": "soon", "session_names": []}), encoding="utf-8")
    _restart(monkeypatch)
    cfg = config.load()
    assert cfg["telemetry_start_date"] == "" and cfg["session_names"] == {}


# ---- chart history ----


@pytest.mark.parametrize("period", ["today", "cycle", "week", "month", "all"])
def test_each_older_window_ends_the_day_before_the_newer_one_starts(served, period):
    newest = served.get("/api/trends", params={"period": period}).json()["trend_window"]
    older = served.get("/api/trends", params={"period": period, "offset": 1}).json()["trend_window"]
    assert newest["offset"] == 0 and older["offset"] == 1
    assert not newest["has_newer"] and older["has_newer"]
    assert date.fromisoformat(older["to"]) + timedelta(days=1) == date.fromisoformat(newest["from"])
    assert newest["to"] == _day(0) or period in ("week", "month", "all")  # the current bucket ends today


def test_the_latest_window_is_what_the_summary_carries(served):
    summary = served.get("/api/summary", params={"period": "week"}).json()
    trends = served.get("/api/trends", params={"period": "week"}).json()
    assert summary["time_series"] == trends["time_series"]
    assert summary["trend_window"] == trends["trend_window"]


def test_an_older_window_holds_the_older_requests_and_says_when_there_is_no_more(served):
    latest = served.get("/api/trends", params={"period": "today"}).json()
    assert latest["trend_window"]["has_older"] is True  # the 40 day old request is before it
    assert sum(sum(m["requests"] for m in b["models"].values()) for b in latest["time_series"]) == 2

    two_back = served.get("/api/trends", params={"period": "today", "offset": 2}).json()
    assert sum(sum(m["requests"] for m in b["models"].values()) for b in two_back["time_series"]) == 1
    assert two_back["trend_window"]["has_older"] is False


def test_an_offset_outside_the_allowed_range_is_refused(served):
    assert served.get("/api/trends", params={"offset": -1}).status_code == 422


# ---- focused range ----


def test_a_range_focuses_every_total_on_its_days(served):
    period = f"range:{_day(3)}..{_day(1)}"
    summary = served.get("/api/summary", params={"period": period}).json()
    assert summary["period"] == period
    assert summary["totals"]["credits"] == 2.0 and summary["totals"]["requests"] == 1
    assert {s["session_id"] for s in served.get("/api/sessions", params={"period": period}).json()["sessions"]} == {"new"}
    assert served.get("/api/requests", params={"period": period}).json()["total"] == 1
    assert served.get("/api/insights", params={"period": period}).json()["period"] == period
    assert served.get("/api/context", params={"period": period}).json()["period"] == period


def test_a_focused_range_is_drawn_day_by_day_and_compares_with_the_span_before_it(served):
    period = f"range:{_day(3)}..{_day(0)}"
    body = served.get("/api/summary", params={"period": period}).json()
    series = body["time_series"]
    assert [b["date"] for b in series] == [_day(3), _day(2), _day(1), _day(0)]
    assert all(b["date"] == b["end"] for b in series)
    assert body["trend_window"]["has_older"] is False and body["trend_window"]["unit"] == "day"
    assert body["prev_totals"]["requests"] == 0  # the 4 days before hold no request


def test_a_long_range_is_drawn_week_by_week_and_counts_only_its_own_days(served):
    period = f"range:{_day(70)}..{_day(0)}"
    body = served.get("/api/summary", params={"period": period}).json()
    assert body["trend_window"]["unit"] == "week" and len(body["time_series"]) <= 52
    assert sum(sum(m["requests"] for m in b["models"].values()) for b in body["time_series"]) == 3


@pytest.mark.parametrize("period", ["range:2026-13-01..2026-13-02", "range:2026-02-01..2026-01-01", "range:2026-02-01", "range:", "range:a..b"])
def test_a_malformed_range_falls_back_to_the_default_period(served, period):
    assert served.get("/api/summary", params={"period": period}).json()["period"] == "today"
    assert aggregate.parse_range(period) is None


def test_the_csv_filename_of_a_range_is_safe(served):
    resp = served.get("/api/export.csv", params={"period": f"range:{_day(3)}..{_day(0)}"})
    disposition = resp.headers["content-disposition"]
    assert ":" not in disposition.split("filename=")[1] and ".." not in disposition
