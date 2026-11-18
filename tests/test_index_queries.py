"""Golden comparison: /api/requests and /api/sessions answered from the read model equal the in-memory answer.

The records are synthetic and go through the real refresh and read model (stored in the per-test runtime-data
folder); the in-memory path, ``request_explorer.explore`` and ``aggregate.get_sessions`` over the snapshot's
records, is the reference every query is compared with.
"""

from __future__ import annotations

import itertools
from datetime import timedelta
from typing import Any, Callable, Dict, List, Tuple

import pytest
from starlette.testclient import TestClient

from src import aggregate, config, index_queries, read_model, refresh, request_explorer
from src.vscode_parser import RequestRecord

Sources = List[Tuple[str, List[RequestRecord]]]


def _sources(rec: Callable[..., RequestRecord]) -> Sources:
    """Several logs: two projects sharing a name, grouped model spellings, unbilled, unknown tokens, errors."""
    alpha = [
        rec(0, credits=0.5, prompt_tokens=100, elapsed_ms=300),
        rec(1, credits=6.0, model="claude-sonnet-4", prompt_tokens=5000, elapsed_ms=9000),
        rec(2, credits=None, elapsed_ms=None, message="Unbilled RÉFACTOR request"),
        rec(3, credits=1.0, mode="", ts=rec(1).ts),  # same moment as request 1: ties on time
        rec(4, credits=2.0, days_ago=45),  # outside every short period
        rec(5, credits=2.0, days_ago=3, model="Claude Sonnet 4"),
    ]
    beta = [
        rec(n, credits=float(n), project_key="e:\\other\\alpha", project_name="alpha", session_id="sess-b",
            session_title="", mode="ask", ide="Code - Insiders", error=n % 2 == 0, days_ago=n % 3)
        for n in range(10, 16)
    ]
    cli = [
        rec(n, source="cli", ide="Copilot CLI", session_id="cli-1", session_title="Copilot CLI session",
            project_key="cli:gamma", project_name="gamma", project_path=None, model="gpt-5",
            prompt_tokens=None, completion_tokens=None, cache_read_tokens=None, cache_write_tokens=None,
            credits=0.33, usage_source="events.jsonl-allocated", usage_estimated=True, days_ago=1)
        for n in range(20, 24)
    ] + [
        rec(24, source="cli", ide="Copilot CLI", session_id="cli-1", session_title="Copilot CLI session",
            project_key="cli:gamma", project_name="gamma", project_path=None, model="gpt-5",
            cache_read_tokens=4000, cache_write_tokens=200, usage_source="session-store", credits=1.0),
    ]
    old = [rec(30, credits=9.0, session_id="sess-old", session_title="Old session", days_ago=400)]
    return [("vscode:alpha", alpha), ("vscode:beta", beta), ("cli-store:x", cli), ("vscode:old", old)]


@pytest.fixture
def client(rec, settings_file, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real refresh over a fresh read model; the last alpha record is also a copied-log duplicate."""
    sources = _sources(rec)
    duplicate = sources[0][1][0]

    def gather(scan: read_model.Scan) -> Tuple[List[RequestRecord], List[dict]]:
        records: List[RequestRecord] = []
        for key, recs in sources:
            got, _meta = scan.records(key, repr(len(recs)), lambda recs=recs: (list(recs), {}))
            records.extend(got)
        # A copy of alpha's first request, read again from a second file: stored, never counted.
        got, _meta = scan.records("vscode:copy", "1", lambda: ([duplicate], {}))
        scan.uncount("vscode:copy", 0)
        return records, []

    monkeypatch.setattr(aggregate, "_gather", gather)
    monkeypatch.setattr(read_model, "model", read_model.ReadModel())
    monkeypatch.setattr(refresh, "store", refresh.SnapshotStore())
    from app.server import app

    return TestClient(app)


def _expected(path: str, params: Dict[str, Any]) -> Any:
    """The in-memory answer for the snapshot the API just served."""
    records = aggregate.visible(refresh.store._snapshot.records)
    if path == "/api/sessions":
        period = params.get("period", "all")
        return {"period": period, "sessions": aggregate.get_sessions(records, period)}
    q = request_explorer.Query.build(**params)
    return request_explorer.explore(records, q)


def _compare(client: TestClient, path: str, params: Dict[str, Any], spy: List[str]) -> None:
    before = len(spy)
    got = client.get(path, params=params).json()
    assert len(spy) == before + 1, "the read model did not answer %s %s" % (path, params)
    assert got == _expected(path, params), (path, params)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    calls: List[str] = []
    for name in ("explore", "sessions"):
        real = getattr(index_queries, name)

        def wrapped(*a, _real=real, _name=name, **kw):
            calls.append(_name)
            return _real(*a, **kw)

        monkeypatch.setattr(index_queries, name, wrapped)
    return calls


PERIODS = ["today", "week", "month", "cycle", "all", "range:%s..%s"]


def _periods() -> List[str]:
    today = aggregate.date.today()
    lo, hi = today - timedelta(days=2), today
    return [p % (lo.isoformat(), hi.isoformat()) if p.startswith("range") else p for p in PERIODS]


def test_sorts_and_pages_match(client: TestClient, spy: List[str]) -> None:
    for period, sort, order in itertools.product(_periods(), request_explorer.SORTS, request_explorer.ORDERS):
        for page in (1, 2, 9):
            _compare(client, "/api/requests",
                     {"period": period, "sort": sort, "order": order, "page": page, "page_size": 4}, spy)


@pytest.mark.parametrize("params", [
    {"project": "e:\\alpha"},
    {"project": "e:\\other\\alpha", "result": "error"},
    {"model": "Claude Sonnet 4"},
    {"model": "claude-sonnet-4", "mode": "unknown"},
    {"model": "no-such-model"},
    {"mode": "ask", "ide": "Code - Insiders"},
    {"result": "ok", "min_credits": 1.0},
    {"min_credits": 0.4, "max_credits": 2.0},
    {"max_credits": 0.0},
    {"q": "réfactor"},
    {"q": "SESS-B"},
    {"q": "copilot cli"},
    {"q": "nothing matches this"},
    {"project": "e:\\alpha", "mode": "agent", "ide": "Code", "result": "ok", "q": "prompt"},
])
def test_filters_and_facets_match(client: TestClient, spy: List[str], params: Dict[str, Any]) -> None:
    for period in ("week", "all"):
        _compare(client, "/api/requests", {"period": period, **params}, spy)


def test_sessions_match(client: TestClient, spy: List[str]) -> None:
    for period in _periods():
        _compare(client, "/api/sessions", {"period": period}, spy)


def test_start_date_and_session_names_apply(client: TestClient, spy: List[str]) -> None:
    config.save({
        "telemetry_start_date": (aggregate.date.today() - timedelta(days=2)).isoformat(),
        "session_names": {"sess-a": "Renamed alpha", "sess-b": "Named untitled"},
        "model_aliases": {"gpt-5": "claude-sonnet-4"},
    })
    for period in ("week", "all"):
        _compare(client, "/api/sessions", {"period": period}, spy)
        for q in ("", "renamed", "named untitled", "alpha session"):
            _compare(client, "/api/requests", {"period": period, "q": q, "sort": "credits"}, spy)
    sessions = client.get("/api/sessions", params={"period": "all"}).json()["sessions"]
    named = {s["session_id"]: s for s in sessions}
    assert named["sess-a"]["custom_name"] == "Renamed alpha"
    assert named["sess-b"]["custom_name"] is None and named["sess-b"]["title"] == "Named untitled"
    assert "sess-old" not in named


def test_the_copied_duplicate_is_not_counted(client: TestClient) -> None:
    body = client.get("/api/requests", params={"period": "all", "page_size": 100}).json()
    ids = [r["request_id"] for r in body["rows"]]
    assert ids.count("req-000") == 1


def test_a_newer_scan_sends_queries_back_to_the_snapshot(client: TestClient, spy: List[str]) -> None:
    client.get("/api/requests", params={"period": "all"})
    snap = refresh.store._snapshot
    with read_model.model.reading(snap.index) as conn:
        assert conn is not None
    with read_model.model.reading("another scan") as conn:
        assert conn is None
    with read_model.model.reading(None) as conn:
        assert conn is None


def test_an_unchanged_scan_keeps_its_token(client: TestClient) -> None:
    client.get("/api/requests")
    first = refresh.store._snapshot.index
    client.get("/api/requests")
    assert first is not None and refresh.store._snapshot.index == first
