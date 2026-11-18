"""Insights over synthetic records: one group of tests per insight type, plus the thin-evidence states."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from src import aggregate, insights


def _of_type(body: dict, kind: str) -> list:
    return [i for i in body["insights"] if i["type"] == kind]


def _budget(**kw) -> dict:
    base = {
        "cycle_start": "2026-01-01", "cycle_end": "2026-01-31", "days_elapsed": 10, "days_total": 31,
        "allowance_credits": 100, "used_credits": 20.0, "used_pct": 20.0,
        "projected_credits": 62.0, "projected_pct": 62.0, "note": "",
    }
    base.update(kw)
    base["risk"] = aggregate.budget_risk_level(
        base["allowance_credits"], base["used_pct"], base["projected_pct"], base["days_elapsed"])
    return base


@pytest.fixture(autouse=True)
def quiet_budget(monkeypatch):
    """A budget far from its limit, so only the budget tests see a budget insight."""
    monkeypatch.setattr(aggregate, "budget_status", lambda records, today=None: _budget())


def _set_budget(monkeypatch, **kw) -> None:
    monkeypatch.setattr(aggregate, "budget_status", lambda records, today=None: _budget(**kw))


# ---- top_contributor ----


def test_top_model_and_project_name_their_share(rec):
    records = [rec(i, model="claude-sonnet-4.5", project_key="e:\\beta", project_name="beta",
                   session_id="s-%d" % i, credits=3.0) for i in range(4)]
    records += [rec(10 + i, session_id="s-x%d" % i, credits=1.0) for i in range(4)]
    body = insights.build_insights(records, "all")

    found = _of_type(body, "top_contributor")
    by_dim = {("model" if "model" in i["evidence"] else "project"): i for i in found
              if "session" not in i["evidence"]}
    assert by_dim["model"]["evidence"] == {"period": "all", "model": "claude-sonnet-4.5"}
    assert "75.0%" in by_dim["model"]["text"] and "12 of 16" in by_dim["model"]["text"]
    assert by_dim["project"]["evidence"] == {"period": "all", "project": "e:\\beta"}
    assert all(i["severity"] == "info" for i in found)


def test_top_session_names_the_session(rec):
    records = [rec(0, session_id="big", session_title="The big one", credits=8.0)]
    records += [rec(1 + i, session_id="s-%d" % i, credits=1.0) for i in range(4)]
    found = [i for i in _of_type(insights.build_insights(records, "all"), "top_contributor")
             if "session" in i["evidence"]]
    assert len(found) == 1
    assert found[0]["evidence"] == {"period": "all", "session": "big"}
    assert "The big one" in found[0]["text"] and "66.7%" in found[0]["text"]


def test_no_top_contributor_when_there_is_a_single_group(rec):
    body = insights.build_insights([rec(i, session_id="s-%d" % i) for i in range(6)], "all")
    assert [i for i in _of_type(body, "top_contributor") if "session" not in i["evidence"]] == []


def test_no_top_contributor_when_credits_are_too_thin(rec):
    records = [rec(0, model="a", credits=0.3)] + [rec(i, model="b", credits=0.1) for i in range(1, 6)]
    assert _of_type(insights.build_insights(records, "all"), "top_contributor") == []


# ---- period_change ----


def test_credits_up_against_yesterday_is_a_warning(rec):
    records = [rec(i, session_id="s-%d" % i, credits=2.0) for i in range(5)]               # 10 today
    records += [rec(i, days_ago=1, session_id="p-%d" % i, credits=1.0) for i in range(4)]  # 4 yesterday
    [found] = _of_type(insights.build_insights(records, "today"), "period_change")
    assert found["severity"] == "warning"
    assert found["evidence"] == {"period": "today"}
    assert "up 150% on yesterday (10 vs 4)" in found["text"]


def test_credits_down_is_informational(rec):
    records = [rec(i, session_id="s-%d" % i, credits=1.0) for i in range(5)]               # 5 today
    records += [rec(i, days_ago=1, session_id="p-%d" % i, credits=4.0) for i in range(5)]  # 20 yesterday
    [found] = _of_type(insights.build_insights(records, "today"), "period_change")
    assert found["severity"] == "info"
    assert "down 75% on yesterday" in found["text"]


def test_small_change_or_thin_previous_period_says_nothing(rec):
    steady = [rec(i, session_id="s-%d" % i, credits=1.0) for i in range(5)]
    steady += [rec(i, days_ago=1, session_id="p-%d" % i, credits=1.2) for i in range(5)]
    assert _of_type(insights.build_insights(steady, "today"), "period_change") == []

    thin_prev = [rec(i, session_id="s-%d" % i, credits=5.0) for i in range(5)]
    thin_prev += [rec(0, days_ago=1, session_id="p-0", credits=0.5)]
    assert _of_type(insights.build_insights(thin_prev, "today"), "period_change") == []


def test_cycle_and_all_have_no_previous_period(rec):
    records = [rec(i, session_id="s-%d" % i, credits=2.0) for i in range(5)]
    for period in ("cycle", "all"):
        assert _of_type(insights.build_insights(records, period), "period_change") == []


# ---- budget_risk ----


def test_budget_used_up_is_critical(rec, monkeypatch):
    _set_budget(monkeypatch, used_credits=104.0, used_pct=104.0, projected_pct=300.0)
    [found] = _of_type(insights.build_insights([rec(0)], "week"), "budget_risk")
    assert found["severity"] == "critical"
    assert found["evidence"] == {"period": "cycle"}
    assert "104 of 100" in found["text"]


def test_budget_projection_past_allowance_is_a_warning(rec, monkeypatch):
    _set_budget(monkeypatch, used_pct=40.0, projected_credits=124.0, projected_pct=124.0)
    [found] = _of_type(insights.build_insights([rec(0)], "week"), "budget_risk")
    assert found["severity"] == "warning"
    assert "124 credits" in found["text"] and "124.0%" in found["text"]


def test_budget_at_eighty_percent_used_is_an_info_attention_finding(rec, monkeypatch):
    _set_budget(monkeypatch, used_credits=80.0, used_pct=80.0, projected_pct=90.0)
    [found] = _of_type(insights.build_insights([rec(0)], "week"), "budget_risk")
    assert found["severity"] == "info"
    assert "80% of the cycle allowance" in found["text"] and "80 of 100" in found["text"]


def test_budget_just_under_eighty_percent_says_nothing(rec, monkeypatch):
    _set_budget(monkeypatch, used_credits=79.0, used_pct=79.0, projected_pct=90.0)
    assert _of_type(insights.build_insights([rec(0)], "week"), "budget_risk") == []


def test_budget_over_eighty_percent_with_an_overrun_projection_is_one_warning_row(rec, monkeypatch):
    _set_budget(monkeypatch, used_credits=85.0, used_pct=85.0, projected_credits=142.0, projected_pct=142.0)
    found = _of_type(insights.build_insights([rec(0)], "week"), "budget_risk")
    assert [i["severity"] for i in found] == ["warning"]
    assert "On pace for" in found[0]["text"]


@pytest.mark.parametrize(
    "kw, severity",
    [
        (dict(used_pct=40.0, projected_pct=62.0), None),
        (dict(used_credits=85.0, used_pct=85.0, projected_pct=90.0), "info"),
        (dict(used_credits=85.0, used_pct=85.0, projected_pct=142.0), "warning"),
        (dict(used_credits=100.0, used_pct=100.0, projected_pct=200.0), "critical"),
    ],
)
def test_budget_shows_at_most_one_row_for_the_level_reached(rec, monkeypatch, kw, severity):
    _set_budget(monkeypatch, **kw)
    found = _of_type(insights.build_insights([rec(0)], "week"), "budget_risk")
    assert [i["severity"] for i in found] == ([severity] if severity else [])


def test_budget_used_up_is_one_critical_finding_not_two(rec, monkeypatch):
    _set_budget(monkeypatch, used_credits=100.0, used_pct=100.0, projected_pct=200.0)
    found = _of_type(insights.build_insights([rec(0)], "week"), "budget_risk")
    assert [i["severity"] for i in found] == ["critical"]


def test_budget_attention_shows_before_the_projection_is_trusted(rec, monkeypatch):
    _set_budget(monkeypatch, days_elapsed=1, used_credits=85.0, used_pct=85.0, projected_pct=2635.0)
    [found] = _of_type(insights.build_insights([rec(0)], "week"), "budget_risk")
    assert found["severity"] == "info"


def test_budget_projection_waits_for_the_third_day(rec, monkeypatch):
    _set_budget(monkeypatch, days_elapsed=2, projected_pct=400.0)
    assert _of_type(insights.build_insights([rec(0)], "week"), "budget_risk") == []


def test_budget_on_track_or_without_allowance_says_nothing(rec, monkeypatch):
    assert _of_type(insights.build_insights([rec(0)], "week"), "budget_risk") == []
    _set_budget(monkeypatch, allowance_credits=0, used_pct=None, projected_pct=None)
    assert _of_type(insights.build_insights([rec(0)], "week"), "budget_risk") == []


# ---- prompt_composition ----


def test_tool_heavy_prompts_warn_with_the_action(rec, tools_heavy):
    records = [rec(i, session_id="s-%d" % i, prompt_details=tools_heavy) for i in range(4)]
    found = _of_type(insights.build_insights(records, "all"), "prompt_composition")
    [tools] = [i for i in found if i["evidence"]["label"] == "Tool Definitions"]
    assert tools["severity"] == "warning"
    assert tools["evidence"] == {"period": "all", "label": "Tool Definitions"}
    assert "70.0%" in tools["text"] and "MCP servers" in tools["text"]
    assert [i["evidence"]["label"] for i in found] == ["Tool Definitions"]    # 20% and 10% stay quiet


def test_other_dominant_label_is_informational(rec):
    details = [{"category": "System", "label": "System Instructions", "percentageOfPrompt": 60},
               {"category": "User Context", "label": "Messages", "percentageOfPrompt": 40}]
    records = [rec(i, session_id="s-%d" % i, prompt_details=details) for i in range(4)]
    found = _of_type(insights.build_insights(records, "all"), "prompt_composition")
    assert [(i["evidence"]["label"], i["severity"]) for i in found] == [
        ("System Instructions", "info"), ("Messages", "info"),
    ]


def test_composition_needs_enough_covered_requests(rec, tools_heavy):
    records = [rec(i, session_id="s-%d" % i, prompt_details=tools_heavy) for i in range(2)]
    records += [rec(10 + i, session_id="u-%d" % i) for i in range(6)]       # no composition logged
    assert _of_type(insights.build_insights(records, "all"), "prompt_composition") == []


def test_composition_text_states_its_coverage(rec, tools_heavy):
    records = [rec(i, session_id="s-%d" % i, prompt_details=tools_heavy) for i in range(3)]
    records += [rec(10 + i, session_id="u-%d" % i) for i in range(3)]       # half the tokens uncovered
    [tools] = [i for i in _of_type(insights.build_insights(records, "all"), "prompt_composition")
               if i["evidence"]["label"] == "Tool Definitions"]
    assert "50% of this period's tokens carry a breakdown" in tools["text"]


# ---- thin evidence, ordering, endpoint ----


def test_empty_period_is_no_data(rec):
    body = insights.build_insights([rec(0, days_ago=40)], "today")
    assert body["state"] == "no_data"
    assert [i["type"] for i in body["insights"]] == ["no_data"]
    assert body["insights"][0]["evidence"] == {"period": "today"}


def test_sparse_period_is_insufficient_evidence(rec):
    body = insights.build_insights([rec(0), rec(1)], "all")
    assert body["state"] == "insufficient"
    assert [i["type"] for i in body["insights"]] == ["insufficient_evidence"]
    assert "Only 2 requests" in body["insights"][0]["text"]


def test_quiet_period_with_enough_requests_has_no_insights(rec):
    body = insights.build_insights([rec(i, session_id="s-%d" % i) for i in range(6)], "all")
    assert body["state"] == "ok"
    assert body["insights"] == []


def test_insights_are_ordered_by_severity_and_use_declared_vocabulary(rec, monkeypatch, tools_heavy):
    _set_budget(monkeypatch, used_credits=120.0, used_pct=120.0)
    records = [rec(i, session_id="s-%d" % i, prompt_details=tools_heavy, credits=2.0) for i in range(5)]
    body = insights.build_insights(records, "all")
    severities = [i["severity"] for i in body["insights"]]
    assert severities == sorted(severities, key=insights.SEVERITIES.index)
    assert severities[0] == "critical"
    assert {i["type"] for i in body["insights"]} <= set(insights.TYPES)
    assert all(i["severity"] in insights.SEVERITIES and i["text"] for i in body["insights"])


def test_invalid_period_falls_back_to_week(rec):
    assert insights.build_insights([rec(0)], "decade")["period"] == "week"


def test_api_insights_endpoint(rec, monkeypatch, tools_heavy):
    from app.server import app

    records = [rec(i, session_id="s-%d" % i, prompt_details=tools_heavy) for i in range(5)]
    monkeypatch.setattr(aggregate, "gather_records", lambda: (records, []))
    body = TestClient(app).get("/api/insights", params={"period": "all"}).json()
    assert body["state"] == "ok"
    assert body["coverage"]["requests_covered"] == 5
    assert _of_type(body, "prompt_composition")
