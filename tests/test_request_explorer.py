"""Request explorer over synthetic records: filters, sorting, pagination, facets and the unbilled rule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import pytest
from starlette.testclient import TestClient

from src import aggregate, request_explorer
from src.request_explorer import Query, explore
from src.vscode_parser import RequestRecord

NOW = datetime.now(tz=timezone.utc)


def _rec(n: int, **kw) -> RequestRecord:
    base = dict(
        ide="Code",
        source="vscode",
        project_key="e:\\alpha",
        project_name="alpha",
        project_path="E:\\alpha",
        session_id="sess-a",
        session_title="Alpha session",
        request_id="req-%02d" % n,
        ts=NOW - timedelta(minutes=n),
        message="prompt number %d" % n,
        mode="agent",
        model_requested="copilot/gpt-5-mini",
        model="gpt-5-mini",
        prompt_tokens=1000,
        completion_tokens=100,
        credits=1.0,
        elapsed_ms=1000,
    )
    base.update(kw)
    return RequestRecord(**base)


def _fixture() -> List[RequestRecord]:
    return [
        _rec(1, credits=0.5, prompt_tokens=100, elapsed_ms=300),
        _rec(2, credits=6.0, model="claude-sonnet", prompt_tokens=5000, elapsed_ms=9000),
        _rec(3, credits=None, elapsed_ms=None, message="unbilled refactor request"),
        _rec(4, credits=12.0, project_key="e:\\beta", project_name="beta", session_id="sess-b",
             session_title="Beta session", mode="ask", ide="Code - Insiders", error=True),
        _rec(5, credits=3.0, project_key="e:\\beta", project_name="beta", session_id="sess-b",
             session_title="Beta session", model="claude-sonnet", mode="edit"),
        _rec(6, credits=2.0, ts=NOW - timedelta(days=45)),  # outside the week
    ]


def _ids(body: dict) -> List[str]:
    return [row["request_id"] for row in body["rows"]]


def _run(**kw) -> dict:
    kw.setdefault("period", "week")
    return explore(_fixture(), Query.build(**kw))


def test_no_filter_returns_every_request_of_the_period_newest_first():
    body = _run()
    assert _ids(body) == ["req-01", "req-02", "req-03", "req-04", "req-05"]
    assert body["total"] == 5
    assert body["period_total"] == 5


@pytest.mark.parametrize("kw, expected", [
    ({"project": "e:\\beta"}, ["req-04", "req-05"]),
    ({"model": "claude-sonnet"}, ["req-02", "req-05"]),
    ({"mode": "ask"}, ["req-04"]),
    ({"ide": "Code - Insiders"}, ["req-04"]),
    ({"result": "error"}, ["req-04"]),
    ({"result": "ok"}, ["req-01", "req-02", "req-03", "req-05"]),
    ({"q": "UNBILLED"}, ["req-03"]),
    ({"q": "beta session"}, ["req-04", "req-05"]),
    ({"project": "e:\\beta", "model": "claude-sonnet"}, ["req-05"]),
])
def test_each_filter_narrows_the_rows(kw, expected):
    assert _ids(_run(**kw)) == expected


def test_credit_range_applies_to_billed_requests_only():
    body = _run(min_credits=3.0)
    assert _ids(body) == ["req-02", "req-04", "req-05"]
    assert "req-03" not in _ids(body)

    body = _run(max_credits=1.0)
    assert _ids(body) == ["req-01"]  # the unbilled request is not "under 1 credit"

    body = _run(min_credits=0.5, max_credits=6.0)
    assert _ids(body) == ["req-01", "req-02", "req-05"]


def test_unbilled_rows_stay_visible_without_a_credit_range():
    assert "req-03" in _ids(_run())
    assert "req-03" in _ids(_run(model="gpt-5-mini"))


def test_sorting_orders_by_the_chosen_column_and_puts_missing_values_last():
    assert _ids(_run(sort="credits", order="desc")) == ["req-04", "req-02", "req-05", "req-01", "req-03"]
    assert _ids(_run(sort="credits", order="asc")) == ["req-01", "req-05", "req-02", "req-04", "req-03"]
    assert _ids(_run(sort="elapsed", order="desc"))[-1] == "req-03"
    assert _ids(_run(sort="elapsed", order="asc"))[-1] == "req-03"
    assert _ids(_run(sort="tokens", order="desc"))[0] == "req-02"
    assert _ids(_run(sort="ts", order="asc")) == ["req-05", "req-04", "req-03", "req-02", "req-01"]


def test_pagination_slices_and_clamps_the_page():
    first = _run(page_size=2, page=1)
    assert _ids(first) == ["req-01", "req-02"]
    assert (first["total"], first["pages"]) == (5, 3)

    last = _run(page_size=2, page=3)
    assert _ids(last) == ["req-05"]

    beyond = _run(page_size=2, page=99)
    assert _ids(beyond) == ["req-05"]
    assert beyond["query"]["page"] == 3


def test_empty_result_is_a_single_empty_page():
    body = _run(q="nothing matches this")
    assert body["rows"] == []
    assert (body["total"], body["pages"]) == (0, 1)


def test_period_scopes_rows_and_facets():
    week = _run(period="week")
    allp = _run(period="all")
    assert allp["total"] == 6 and week["total"] == 5
    week_models = {o["value"]: o["count"] for o in week["facets"]["model"]}
    all_models = {o["value"]: o["count"] for o in allp["facets"]["model"]}
    assert week_models == {"gpt-5-mini": 3, "claude-sonnet": 2}
    assert all_models == {"gpt-5-mini": 4, "claude-sonnet": 2}


def test_facets_count_what_picking_each_option_would_show():
    body = _run(project="e:\\beta")
    models = {o["value"]: o["count"] for o in body["facets"]["model"]}
    assert models == {"gpt-5-mini": 1, "claude-sonnet": 1}  # beta's own rows
    projects = {o["value"]: o["count"] for o in body["facets"]["project"]}
    # the project facet ignores its own selection: alpha would show 3, beta 2
    assert projects == {"e:\\alpha": 3, "e:\\beta": 2}
    labels = {o["value"]: o["label"] for o in body["facets"]["project"]}
    assert labels["e:\\beta"] == "beta"


def test_a_selected_option_with_no_rows_stays_listed_at_zero():
    body = _run(project="e:\\beta", model="gpt-5-mini", mode="agent")
    assert _ids(body) == []
    projects = {o["value"]: o["count"] for o in body["facets"]["project"]}
    assert projects["e:\\beta"] == 0


def test_result_facet_counts_ok_and_error():
    body = _run()
    results = {o["value"]: o["count"] for o in body["facets"]["result"]}
    assert results == {"ok": 4, "error": 1}


def test_match_totals_sum_billed_credits_only():
    body = _run(model="gpt-5-mini")
    assert body["credits"] == pytest.approx(0.5 + 12.0)


def test_rows_carry_a_short_preview_and_the_session_link_fields():
    long_message = "word " * 100
    body = explore([_rec(1, message=long_message)], Query.build())
    row = body["rows"][0]
    assert len(row["message"]) <= request_explorer.PREVIEW_CHARS
    assert row["message"].endswith("…")
    assert row["session_id"] == "sess-a"
    assert row["usd"] == pytest.approx(0.01)


def test_unknown_values_fall_back_to_a_valid_query():
    q = Query.build(period="bogus", sort="nope", order="sideways", result="maybe", page=0, page_size=10_000)
    assert (q.period, q.sort, q.order, q.result) == ("week", "ts", "desc", "")
    assert (q.page, q.page_size) == (1, request_explorer.MAX_PAGE_SIZE)


def test_api_route_filters_sorts_and_pages(monkeypatch: pytest.MonkeyPatch):
    from app.server import app

    monkeypatch.setattr(aggregate, "gather_records", lambda: (_fixture(), []))
    client = TestClient(app)

    resp = client.get("/api/requests", params={
        "period": "week", "project": "e:\\beta", "sort": "credits", "order": "desc", "page_size": 1,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert [row["request_id"] for row in body["rows"]] == ["req-04"]
    assert (body["total"], body["pages"]) == (2, 2)
    assert body["query"]["project"] == "e:\\beta"

    unbilled = client.get("/api/requests", params={"period": "week", "min_credits": 1}).json()
    assert "req-03" not in [row["request_id"] for row in unbilled["rows"]]

    assert client.get("/api/requests", params={"page_size": 0}).status_code == 422
    assert client.get("/api/requests", params={"min_credits": -1}).status_code == 422
