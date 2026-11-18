"""Context drill-down over synthetic records: contributors, shares, coverage and the no-data states."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from src import aggregate, context_drilldown


def _details(tools_pct: int) -> list:
    return [
        {"category": "System", "label": "Tool Definitions", "percentageOfPrompt": tools_pct},
        {"category": "User Context", "label": "Messages", "percentageOfPrompt": 100 - tools_pct},
    ]


@pytest.fixture
def records(rec):
    return [
        rec(0, prompt_tokens=10_000, prompt_details=_details(50)),                       # alpha: 5k tools
        rec(1, session_id="sess-b", session_title="Beta session", project_key="e:\\beta",
            project_name="beta", model="claude-sonnet-4.5",
            prompt_tokens=30_000, prompt_details=_details(100)),                        # beta: 30k tools
        rec(2, session_id="sess-b", session_title="Beta session", project_key="e:\\beta",
            project_name="beta", model="claude-sonnet-4.5",
            prompt_tokens=10_000, prompt_details=_details(50)),                         # beta: 5k tools
        rec(3, prompt_tokens=20_000),                                                    # no composition
    ]


def test_label_contributors_with_shares_and_coverage(records):
    body = context_drilldown.drilldown(records, "all", "Tool Definitions")

    assert body["state"] == "ok"
    assert body["total_est_tokens"] == 40_000
    assert [(p["project"], p["est_tokens"], p["share_pct"]) for p in body["projects"]] == [
        ("beta", 35_000, 87.5), ("alpha", 5_000, 12.5),
    ]
    assert [(m["model"], m["share_pct"]) for m in body["models"]] == [
        ("claude-sonnet-4.5", 87.5), ("gpt-5-mini", 12.5),
    ]
    assert body["sessions"][0]["session_id"] == "sess-b"
    assert body["sessions"][0]["requests"] == 2
    assert body["requests"][0]["request_id"] == "req-001"
    assert body["requests"][0]["est_tokens"] == 30_000

    cov = body["coverage"]
    assert (cov["requests_covered"], cov["requests_total"]) == (3, 4)
    assert cov["requests_pct"] == 75.0
    assert (cov["tokens_covered"], cov["tokens_total"]) == (50_000, 70_000)
    assert cov["tokens_pct"] == 71.4
    assert "Estimates" in body["note"]


def test_empty_label_drills_into_the_whole_covered_prompt(records):
    body = context_drilldown.drilldown(records, "all", "")
    assert body["total_est_tokens"] == 50_000
    assert body["projects"][0]["project"] == "beta"
    assert body["projects"][0]["share_pct"] == 80.0
    assert {row["label"] for row in body["labels"]} == {"Tool Definitions", "Messages"}


def test_session_contributors_carry_the_diagnostic_flags(rec):
    big = [rec(i, prompt_tokens=60_000, prompt_details=_details(50)) for i in range(2)]
    body = context_drilldown.drilldown(big, "all", "")
    assert [f["type"] for f in body["sessions"][0]["flags"]] == ["large_prompt"]


def test_contributor_lists_are_capped(rec):
    many = [
        rec(i, session_id="s-%d" % i, project_key="p-%d" % i, project_name="p-%d" % i,
            prompt_tokens=1_000 + i, prompt_details=_details(50))
        for i in range(20)
    ]
    body = context_drilldown.drilldown(many, "all", "")
    for key in ("projects", "sessions", "requests"):
        assert len(body[key]) == context_drilldown.TOP_N
    assert body["projects"][0]["project"] == "p-19"


def test_period_with_no_requests_is_no_data():
    empty = context_drilldown.drilldown([], "week", "")
    assert empty["state"] == "no_data"
    assert empty["reason"] == "No requests in this period."
    assert empty["coverage"]["requests_pct"] is None
    assert empty["projects"] == []


def test_requests_without_any_composition_are_no_data_with_a_reason(rec):
    body = context_drilldown.drilldown([rec(0), rec(1)], "all", "")
    assert body["state"] == "no_data"
    assert "composition" in body["reason"]
    assert body["coverage"]["requests_covered"] == 0
    assert body["coverage"]["requests_pct"] == 0.0


def test_unknown_label_is_no_data(records):
    body = context_drilldown.drilldown(records, "all", "Attachments")
    assert body["state"] == "no_data"
    assert "Attachments" in body["reason"]


def test_invalid_period_falls_back_to_week(records):
    assert context_drilldown.drilldown(records, "decade", "")["period"] == "week"


def test_api_context_endpoint(records, monkeypatch):
    from app.server import app

    monkeypatch.setattr(aggregate, "gather_records", lambda: (records, []))
    body = TestClient(app).get("/api/context", params={"period": "all", "label": "Tool Definitions"}).json()
    assert body["state"] == "ok"
    assert body["label"] == "Tool Definitions"
    assert body["projects"][0]["project"] == "beta"


def test_context_items_rank_by_requests_and_are_capped_to_twenty(rec):
    records = [
        rec(0, context_items=["a.py", "tool-x"]),
        rec(1, context_items=["a.py", "a.py"]),            # a repeat inside one request counts once
        rec(2, context_items=["a.py"] + ["f%02d" % i for i in range(30)]),
        rec(3, days_ago=40, context_items=["old.py"]),    # outside the week
    ]
    body = context_drilldown.drilldown(records, "week", "")
    items = body["context_items"]

    assert items[0] == {"label": "a.py", "requests": 3}
    assert len(items) == context_drilldown.CONTEXT_ITEMS_TOP == 20
    assert all(i["label"] != "old.py" for i in items)
    assert [i["requests"] for i in items] == sorted((i["requests"] for i in items), reverse=True)


def test_context_items_present_even_when_no_composition_is_logged(rec):
    body = context_drilldown.drilldown([rec(0, context_items=["b.md"])], "all", "")
    assert body["state"] == "no_data"
    assert body["context_items"] == [{"label": "b.md", "requests": 1}]


def test_period_without_context_items_returns_an_empty_list(records):
    assert context_drilldown.drilldown(records, "all", "")["context_items"] == []
    assert context_drilldown.drilldown([], "all", "")["context_items"] == []
