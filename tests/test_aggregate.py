"""Aggregation tests over synthetic RequestRecords."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src import aggregate
from src.vscode_parser import RequestRecord


def _rec(**kw) -> RequestRecord:
    base = dict(
        ide="Code",
        source="vscode",
        project_key="e:\\proj",
        project_name="proj",
        project_path="E:\\proj",
        session_id="sess-1",
        session_title="A session",
        request_id="req-1",
        ts=datetime.now(tz=timezone.utc),
        message="hello",
        mode="agent",
        model_requested="copilot/gpt-5-mini",
        model="gpt-5-mini",
        prompt_tokens=1000,
        completion_tokens=100,
        credits=0.5,
        elapsed_ms=1200,
    )
    base.update(kw)
    return RequestRecord(**base)


def test_summary_totals_and_breakdowns():
    records = [
        _rec(),
        _rec(request_id="req-2", model="claude-sonnet-4.5", credits=1.0),
        _rec(request_id="req-3", credits=None),  # unbilled
        _rec(request_id="req-4", ts=datetime.now(tz=timezone.utc) - timedelta(days=40)),
    ]
    body = aggregate.get_summary(records, "today")
    t = body["totals"]
    assert t["requests"] == 3
    assert t["billed_requests"] == 2
    assert t["credits"] == pytest.approx(1.5)
    assert t["usd"] == pytest.approx(0.015)
    assert t["prompt_tokens"] == 3000

    models = {m["model"]: m for m in body["by_model"]}
    assert set(models) == {"gpt-5-mini", "claude-sonnet-4.5"}
    assert models["claude-sonnet-4.5"]["credits_share_pct"] == pytest.approx(66.7)

    assert body["by_project"][0]["project"] == "proj"
    assert body["budget"]["allowance_credits"] > 0


def test_prompt_mix_weighted_by_prompt_tokens():
    details_a = [
        {"category": "System", "label": "Tool Definitions", "percentageOfPrompt": 50},
        {"category": "User Context", "label": "Messages", "percentageOfPrompt": 50},
    ]
    details_b = [
        {"category": "System", "label": "Tool Definitions", "percentageOfPrompt": 100},
    ]
    records = [
        _rec(prompt_tokens=1000, prompt_details=details_a),
        _rec(request_id="r2", prompt_tokens=3000, prompt_details=details_b),
    ]
    mix = aggregate.prompt_mix(records)
    by_label = {m["label"]: m for m in mix}
    # Tool Definitions: 1000*0.5 + 3000*1.0 = 3500 of 4000 covered = 87.5%
    assert by_label["Tool Definitions"]["pct"] == pytest.approx(87.5)
    assert by_label["Tool Definitions"]["est_tokens"] == 3500
    assert by_label["Messages"]["pct"] == pytest.approx(12.5)


def test_sessions_grouping_and_detail():
    t0 = datetime.now(tz=timezone.utc)
    records = [
        _rec(ts=t0 - timedelta(minutes=10)),
        _rec(request_id="req-2", ts=t0, model="claude-sonnet-4.5", credits=2.0),
        _rec(session_id="sess-2", request_id="req-3", session_title="Other"),
    ]
    sessions = aggregate.build_sessions(records)
    assert len(sessions) == 2
    top = sessions[0]  # sorted by credits desc
    assert top["session_id"] == "sess-1"
    assert top["credits"] == pytest.approx(2.5)
    assert set(top["models"]) == {"gpt-5-mini", "claude-sonnet-4.5"}

    detail = aggregate.get_session_detail(records, "sess-1")
    assert detail is not None
    assert len(detail["requests_detail"]) == 2
    assert detail["requests_detail"][0]["ts"] <= detail["requests_detail"][1]["ts"]
    assert aggregate.get_session_detail(records, "nope") is None


def test_cycle_start_respects_reset_day(monkeypatch):
    monkeypatch.setattr(
        aggregate.config, "load",
        lambda: {"cycle_reset_day": 15, "monthly_credits": 300},
    )
    assert aggregate.cycle_start(date(2026, 7, 20)) == date(2026, 7, 15)
    assert aggregate.cycle_start(date(2026, 7, 10)) == date(2026, 6, 15)


@pytest.mark.parametrize("allowance, used_pct, projected_pct, days, expected", [
    (100, 79.0, 90.0, 10, "none"),
    (100, 80.0, 90.0, 10, "attention"),
    (100, 50.0, 101.0, 10, "warning"),
    (100, 50.0, 400.0, 2, "none"),
    (100, 85.0, 2600.0, 2, "attention"),
    (100, 100.0, 120.0, 10, "critical"),
    (0, None, None, 10, "none"),
])
def test_budget_risk_level(allowance, used_pct, projected_pct, days, expected):
    assert aggregate.budget_risk_level(allowance, used_pct, projected_pct, days) == expected


def test_budget_status_carries_the_risk_level(monkeypatch):
    today = date(2026, 7, 2)
    monkeypatch.setattr(aggregate.config, "load", lambda: {"monthly_credits": 10, "cycle_reset_day": 1})
    used = _rec(ts=datetime(2026, 7, 1, 12, 0), credits=8.5)
    assert aggregate.budget_status([used], today)["risk"] == "attention"
    monkeypatch.setattr(aggregate.config, "load", lambda: {"monthly_credits": 0, "cycle_reset_day": 1})
    quiet = aggregate.budget_status([used], today)
    assert quiet["risk"] == "none" and quiet["used_pct"] is None and quiet["projected_pct"] is None


def test_export_csv_shape():
    csv_text = aggregate.export_csv([_rec()], "all")
    lines = csv_text.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("timestamp,ide,source,project")
    assert "gpt-5-mini" in lines[1]
    assert "0.5" in lines[1]


def test_token_definitions():
    # input volume 500 = uncached 200 + cache-write 300; cache-read 1000 sits outside it.
    m = aggregate.token_metrics(500, 40, 1000, 300, 2.0)
    assert m["uncached_input_tokens"] == 200
    assert m["total_input_tokens"] == 1500
    assert m["effective_input_tokens"] == 300       # 200 + 10% of 1000
    assert m["effective_output_tokens"] == 70       # 40 + 10% of 300
    assert m["tokens_per_credit"] == pytest.approx(185.0)
    assert aggregate.token_metrics(500, 40, 0, 0, 0.0)["tokens_per_credit"] is None
    assert aggregate.token_metrics(500, 40, 0, 0, None)["tokens_per_credit"] is None


def test_cache_buckets_and_estimates_reach_totals_and_detail():
    records = [
        _rec(source="cli", prompt_tokens=500, cache_read_tokens=1000, cache_write_tokens=300,
             credits=2.0, usage_source="events.jsonl-allocated", usage_estimated=True),
        _rec(request_id="req-2", usage_source="vscode"),
    ]
    t = aggregate.get_summary(records, "today")["totals"]
    assert (t["prompt_tokens"], t["cache_read_tokens"], t["cache_write_tokens"]) == (1500, 1000, 300)
    assert t["total_input_tokens"] == 2500
    assert t["estimated_requests"] == 1

    detail = aggregate.get_session_detail(records, "sess-1")
    est = detail["requests_detail"][0]
    assert est["usage_estimated"] is True and est["usage_source"] == "events.jsonl-allocated"
    assert est["uncached_input_tokens"] == 200
    assert detail["usage_sources"] == ["events.jsonl-allocated", "vscode"]


def test_unknown_tokens_stay_apart_from_zero_in_totals():
    unknown = dict(prompt_tokens=None, completion_tokens=None, cache_read_tokens=None, cache_write_tokens=None)
    only_unknown = aggregate.get_summary([_rec(**unknown)], "today")["totals"]
    assert only_unknown["requests"] == 1
    assert only_unknown["unknown_token_requests"] == 1
    for field in ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens",
                  "uncached_input_tokens", "total_input_tokens", "effective_input_tokens",
                  "effective_output_tokens", "tokens_per_credit"):
        assert only_unknown[field] is None, field

    mixed = aggregate.get_summary([_rec(**unknown), _rec(request_id="req-2")], "today")["totals"]
    assert mixed["unknown_token_requests"] == 1
    assert (mixed["prompt_tokens"], mixed["completion_tokens"]) == (1000, 100)
    assert mixed["tokens_per_credit"] is not None

    recorded_zero = aggregate.get_summary([_rec(prompt_tokens=0, completion_tokens=0)], "today")["totals"]
    assert recorded_zero["prompt_tokens"] == 0 and recorded_zero["unknown_token_requests"] == 0


def test_unknown_tokens_reach_the_session_sheet_trends_and_csv():
    unknown = _rec(prompt_tokens=None, completion_tokens=None, cache_read_tokens=None, cache_write_tokens=None,
                   source="cli", usage_source="unavailable")
    detail = aggregate.get_session_detail([unknown], "sess-1")
    row = detail["requests_detail"][0]
    assert row["prompt_tokens"] is None and row["completion_tokens"] is None
    assert row["uncached_input_tokens"] is None and row["usage_source"] == "unavailable"
    assert detail["unknown_token_requests"] == 1 and detail["prompt_tokens"] is None

    slot = aggregate.get_trends([unknown], "today")["time_series"][-1]["models"]["gpt-5-mini"]
    assert slot["prompt_tokens"] is None and slot["requests"] == 1

    cells = aggregate.export_csv([unknown], "all").strip().splitlines()[1].split(",")
    assert "None" not in cells
