"""Aggregation tests over synthetic RequestRecords."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app import aggregate
from app.vscode_parser import RequestRecord


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


def test_export_csv_shape():
    csv_text = aggregate.export_csv([_rec()], "all")
    lines = csv_text.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("timestamp,ide,source,project")
    assert "gpt-5-mini" in lines[1]
    assert "0.5" in lines[1]
