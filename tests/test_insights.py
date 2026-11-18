"""Insights over synthetic records: one group of tests per insight type, each threshold at and just past its boundary, and the four states."""

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


def _by_dim(body: dict) -> dict:
    return {
        ("session" if "session" in i["evidence"] else "model" if "model" in i["evidence"] else "project"): i
        for i in _of_type(body, "top_contributor")
    }


def test_top_model_and_project_name_their_share(rec):
    records = [rec(i, model="claude-sonnet-4.5", project_key="e:\\beta", project_name="beta",
                   session_id="s-%d" % i, credits=3.0) for i in range(4)]
    records += [rec(10 + i, session_id="s-x%d" % i, credits=1.0) for i in range(4)]
    by_dim = _by_dim(insights.build_insights(records, "all"))

    assert by_dim["model"]["evidence"] == {"period": "all", "model": "claude-sonnet-4.5"}
    assert "75.0%" in by_dim["model"]["text"] and "12 of 16" in by_dim["model"]["text"]
    assert by_dim["project"]["evidence"] == {"period": "all", "project": "e:\\beta"}
    assert by_dim["model"]["severity"] == by_dim["project"]["severity"] == "attention"


def test_top_session_names_the_session(rec):
    records = [rec(0, session_id="big", session_title="The big one", credits=8.0)]
    records += [rec(1 + i, session_id="s-%d" % i, credits=1.0) for i in range(4)]
    found = _by_dim(insights.build_insights(records, "all"))["session"]
    assert found["evidence"] == {"period": "all", "session": "big"}
    assert "The big one" in found["text"] and "66.7%" in found["text"]


def test_the_largest_contributor_is_named_whenever_the_period_has_credits(rec):
    """One request, one model, one project, one session: each is still the largest, at 100%."""
    by_dim = _by_dim(insights.build_insights([rec(0, credits=0.3)], "all"))
    assert set(by_dim) == {"model", "project", "session"}
    assert all(i["severity"] == "attention" for i in by_dim.values())


def test_a_period_without_billed_credits_names_no_contributor(rec):
    records = [rec(i, credits=0.0, session_id="s-%d" % i) for i in range(6)]
    assert _of_type(insights.build_insights(records, "all"), "top_contributor") == []


@pytest.mark.parametrize(
    "credits, severity",
    [
        ([50.0, 25.0, 25.0], "attention"),      # exactly half the spend
        ([49.96, 25.02, 25.02], "info"),        # rounds to 50.0 for display, still under half
    ],
)
def test_a_contributor_is_attention_at_half_the_spend_and_info_just_below(rec, credits, severity):
    records = [
        rec(i, model="m-%d" % i, project_key="p-%d" % i, project_name="p-%d" % i, session_id="s-%d" % i, credits=c)
        for i, c in enumerate(credits)
    ]
    by_dim = _by_dim(insights.build_insights(records, "all"))
    assert {k: v["severity"] for k, v in by_dim.items()} == {"model": severity, "project": severity, "session": severity}


# ---- period_change ----


def _two_periods(rec, now: float, before: float) -> list:
    """Credits today and yesterday, each on one request (before == 0 leaves yesterday empty)."""
    records = [rec(0, session_id="now", credits=now)]
    if before:
        records.append(rec(0, days_ago=1, session_id="before", credits=before))
    return records


def test_credits_up_against_yesterday_is_a_warning(rec):
    [found] = _of_type(insights.build_insights(_two_periods(rec, 10.0, 4.0), "today"), "period_change")
    assert found["severity"] == "warning"
    assert found["evidence"] == {"period": "today"}
    assert "up 150% on yesterday (10 vs 4)" in found["text"]


def test_credits_down_is_informational(rec):
    [found] = _of_type(insights.build_insights(_two_periods(rec, 5.0, 20.0), "today"), "period_change")
    assert found["severity"] == "info"
    assert "down 75% on yesterday" in found["text"]


@pytest.mark.parametrize(
    "now, before, shown",
    [
        (12.0, 10.0, True),        # exactly 20% of the previous spend (2 credits)
        (11.9, 10.0, False),       # just under
        (8.0, 10.0, True),         # a fall counts the same way
        (8.1, 10.0, False),
        (3.0, 2.5, True),          # a small period: the 0.5-credit floor is the larger cut
        (2.9, 2.5, False),
        (2.0, 2.5, True),
        (0.5, 0.0, True),          # nothing before: the period itself must reach 0.5 credits
        (0.4, 0.0, False),
    ],
)
def test_a_change_shows_from_the_larger_of_half_a_credit_and_a_fifth_of_the_previous_spend(rec, now, before, shown):
    found = _of_type(insights.build_insights(_two_periods(rec, now, before), "today"), "period_change")
    assert bool(found) is shown


def test_a_change_from_nothing_states_it_without_a_percentage(rec):
    [found] = _of_type(insights.build_insights(_two_periods(rec, 3.0, 0.0), "today"), "period_change")
    assert found["severity"] == "warning"
    assert "%" not in found["text"] and "against none" in found["text"]


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


def _mix(user_pct: int, label: str = "Tool Definitions") -> list:
    return [{"category": "System", "label": label, "percentageOfPrompt": 100 - user_pct},
            {"category": "User Context", "label": "Messages", "percentageOfPrompt": user_pct}]


def test_tool_heavy_prompts_warn_with_the_action(rec, tools_heavy):
    records = [rec(i, session_id="s-%d" % i, prompt_details=tools_heavy) for i in range(4)]
    [tools] = _of_type(insights.build_insights(records, "all"), "prompt_composition")
    assert tools["severity"] == "warning"
    assert tools["evidence"] == {"period": "all", "label": "Tool Definitions"}
    assert "70.0%" in tools["text"] and "90.0% is outside your own context" in tools["text"]
    assert "MCP servers" in tools["text"]


def test_another_dominant_overhead_label_is_informational(rec):
    records = [rec(i, session_id="s-%d" % i, prompt_details=_mix(20, "System Instructions")) for i in range(4)]
    [found] = _of_type(insights.build_insights(records, "all"), "prompt_composition")
    assert (found["evidence"]["label"], found["severity"]) == ("System Instructions", "info")


@pytest.mark.parametrize("user_pct, shown", [(30, True), (31, False)])
def test_composition_needs_seventy_percent_outside_the_users_own_context(rec, user_pct, shown):
    records = [rec(i, session_id="s-%d" % i, prompt_details=_mix(user_pct)) for i in range(4)]
    assert bool(_of_type(insights.build_insights(records, "all"), "prompt_composition")) is shown


@pytest.mark.parametrize("covered, shown", [(2, True), (1, False)])
def test_composition_needs_two_covered_requests(rec, tools_heavy, covered, shown):
    records = [rec(i, session_id="s-%d" % i, prompt_details=tools_heavy) for i in range(covered)]
    assert bool(_of_type(insights.build_insights(records, "all"), "prompt_composition")) is shown


@pytest.mark.parametrize("uncovered, shown", [(2, True), (3, False)])
def test_composition_needs_half_the_prompt_tokens_covered(rec, tools_heavy, uncovered, shown):
    records = [rec(i, session_id="s-%d" % i, prompt_details=tools_heavy) for i in range(2)]   # 20k covered
    records += [rec(10 + i, session_id="u-%d" % i) for i in range(uncovered)]                  # 10k each, none
    found = _of_type(insights.build_insights(records, "all"), "prompt_composition")
    assert bool(found) is shown
    if shown:
        assert "50% of this period's tokens carry a breakdown" in found[0]["text"]


# ---- states, ordering, endpoint ----


def test_empty_period_is_no_data(rec):
    body = insights.build_insights([rec(0, days_ago=40)], "today")
    assert body["state"] == "no_data"
    assert [i["type"] for i in body["insights"]] == ["no_data"]
    assert body["insights"][0]["evidence"] == {"period": "today"}


def test_requests_that_billed_nothing_are_insufficient_data(rec):
    body = insights.build_insights([rec(0, credits=0.0), rec(1, credits=0.0)], "all")
    assert body["state"] == "insufficient_data"
    assert [i["type"] for i in body["insights"]] == ["insufficient_data"]
    assert "2 requests" in body["insights"][0]["text"]


def test_a_period_with_credits_is_ready_even_with_one_request(rec):
    body = insights.build_insights([rec(0)], "all")
    assert body["state"] == "ready"
    assert len(_of_type(body, "top_contributor")) == 3


def test_a_period_where_every_rule_is_quiet_has_no_actionable_findings(rec, monkeypatch):
    monkeypatch.setattr(insights, "_top_contributors", lambda summary, period: [])
    body = insights.build_insights([rec(i, session_id="s-%d" % i) for i in range(6)], "all")
    assert body["state"] == "no_actionable_findings"
    assert body["insights"] == []


def test_state_is_always_one_of_the_declared_four(rec):
    for records in ([], [rec(0, credits=0.0)], [rec(0)]):
        assert insights.build_insights(records, "all")["state"] in insights.STATES


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
    assert body["state"] == "ready"
    assert body["coverage"]["requests_covered"] == 5
    assert _of_type(body, "prompt_composition")
