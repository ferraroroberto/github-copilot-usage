"""The shared refresh: one rebuild for joined callers, survival of a cancelled caller, ordering, and the cached summary."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import Callable, List

import pytest
from fastapi.testclient import TestClient

from src import aggregate, refresh
from src.refresh import SnapshotStore


class GatedBuild:
    """A builder that blocks until released, and counts how often it ran: an injected delay, no timing race."""

    def __init__(self, records_for: Callable[[int], list]) -> None:
        self.records_for = records_for
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self):
        self.calls += 1
        n = self.calls
        self.started.set()
        assert self.release.wait(10), "the test never released the build"
        return self.records_for(n), [{"ide": "Code"}]


async def _until_started(build: GatedBuild) -> None:
    while not build.started.is_set():
        await asyncio.sleep(0.005)


def test_concurrent_refreshes_share_one_rebuild(rec):
    build = GatedBuild(lambda n: [rec(0)])
    store = SnapshotStore(build=build, clear_caches=lambda: None)

    async def scenario():
        first = asyncio.ensure_future(store.refresh())
        await _until_started(build)
        second = asyncio.ensure_future(store.refresh())
        third = asyncio.ensure_future(store.refresh())
        await asyncio.sleep(0.02)
        build.release.set()
        return await asyncio.gather(first, second, third)

    outcomes = asyncio.run(scenario())
    assert build.calls == 1
    assert [o.joined for o in outcomes] == [False, True, True]
    assert len({o.snapshot.generation for o in outcomes}) == 1


def test_a_cancelled_first_request_does_not_lose_the_refresh(rec):
    build = GatedBuild(lambda n: [rec(0), rec(1)])
    store = SnapshotStore(build=build, clear_caches=lambda: None)

    async def scenario():
        first = asyncio.ensure_future(store.refresh())
        await _until_started(build)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        build.release.set()
        # The next read joins the build the cancelled one started instead of beginning another.
        return await store.snapshot()

    snap = asyncio.run(scenario())
    assert build.calls == 1
    assert len(snap.records) == 2


def test_a_force_refresh_during_an_incremental_one_rebuilds_and_the_older_result_is_dropped(rec):
    cleared: List[bool] = []
    build = GatedBuild(lambda n: [rec(i) for i in range(n)])
    store = SnapshotStore(build=build, clear_caches=lambda: cleared.append(True))

    async def scenario():
        incremental = asyncio.ensure_future(store.refresh())
        await _until_started(build)
        forced = asyncio.ensure_future(store.refresh(force=True))
        await asyncio.sleep(0.02)
        build.release.set()
        await asyncio.gather(incremental, forced)
        return store._snapshot

    final = asyncio.run(scenario())
    # The incremental build (generation 1) ran first; the forced one (2) is what stays installed.
    assert build.calls == 2
    assert final.generation == 2 and final.forced
    assert cleared == [True]


def test_an_older_build_finishing_last_never_overwrites_newer_data(rec):
    store = SnapshotStore(build=lambda: ([], []), clear_caches=lambda: None)
    newer = store._run_build(2, False)
    older = replace(store._run_build(1, False), records=[rec(0)])

    assert store._install(newer) is newer
    assert store._install(older) is newer
    assert store._snapshot is newer


def test_clear_drops_the_snapshot_and_a_build_that_started_before_it(rec):
    wiped: List[bool] = []
    build = GatedBuild(lambda n: [rec(0)])
    store = SnapshotStore(build=build, clear_caches=lambda: wiped.append(True))

    async def scenario():
        running = asyncio.ensure_future(store.refresh())
        await _until_started(build)
        clearing = asyncio.ensure_future(store.clear())
        await asyncio.sleep(0.02)
        build.release.set()
        await asyncio.gather(running, clearing)
        return store._snapshot

    assert asyncio.run(scenario()) is None
    assert wiped == [True]


def test_a_failed_build_is_reported_and_the_next_refresh_retries(rec):
    state = {"fail": True}

    def build():
        if state["fail"]:
            raise OSError("disk went away")
        return [rec(0)], []

    store = SnapshotStore(build=build, clear_caches=lambda: None)

    async def scenario():
        with pytest.raises(OSError):
            await store.refresh()
        state["fail"] = False
        return await store.snapshot()

    assert len(asyncio.run(scenario()).records) == 1


def test_fingerprint_changes_when_only_a_records_credits_change(rec):
    base = [rec(0, credits=1.0), rec(1)]
    edited = [rec(0, credits=2.5), rec(1)]
    assert len(base) == len(edited) and base[-1].ts == edited[-1].ts
    assert refresh.fingerprint(base) != refresh.fingerprint(edited)
    assert refresh.fingerprint(base) == refresh.fingerprint([rec(0, credits=1.0), rec(1)])


def test_summary_is_cached_until_record_content_changes_with_count_and_newest_ts_unchanged(rec):
    data = {"records": [rec(0, credits=1.0), rec(1, credits=1.0)]}
    store = SnapshotStore(build=lambda: (data["records"], []), clear_caches=lambda: None)
    computed: List[int] = []

    def compute(records, period):
        computed.append(1)
        return aggregate.get_summary(records, period)

    async def scenario():
        first = store.summary(await store.snapshot(), "all", compute)
        again = store.summary(await store.snapshot(), "all", compute)
        data["records"] = [rec(0, credits=4.0), rec(1, credits=1.0)]
        edited = store.summary(await store.snapshot(), "all", compute)
        return first, again, edited

    first, again, edited = asyncio.run(scenario())
    assert len(computed) == 2  # the repeat read hit the cache; the edit did not
    assert first["totals"]["credits"] == again["totals"]["credits"] == 2.0
    assert edited["totals"]["credits"] == 5.0


def test_refresh_and_clear_routes_report_their_outcome(monkeypatch, rec):
    from app.server import app

    calls = {"n": 0}

    def gather():
        calls["n"] += 1
        return [rec(0)], [{"ide": "Code"}]

    monkeypatch.setattr(aggregate, "gather_records", gather)
    cleared: List[bool] = []
    monkeypatch.setattr(refresh.store, "_clear_caches", lambda: cleared.append(True))
    client = TestClient(app)

    plain = client.post("/api/refresh").json()
    assert plain["ok"] and not plain["forced"] and plain["requests"] == 1 and plain["sources"] == 1

    forced = client.post("/api/refresh", params={"force": "true"}).json()
    assert forced["ok"] and forced["forced"] and forced["generation"] > plain["generation"]
    assert cleared == [True]

    assert client.post("/api/cache/clear").json() == {"ok": True}
    assert cleared == [True, True]
    # The cleared store rebuilds on the next read instead of serving nothing.
    assert client.get("/api/summary", params={"period": "all"}).status_code == 200


def test_a_failing_refresh_route_returns_the_reason(monkeypatch):
    from app.server import app

    def boom():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(aggregate, "gather_records", boom)
    body = TestClient(app).post("/api/refresh").json()
    assert body["ok"] is False and "unreadable" in body["reason"]


def test_refresh_work_runs_on_the_named_pool_and_the_request_reports_it(monkeypatch, rec):
    from app.server import app

    monkeypatch.setattr(aggregate, "gather_records", lambda: ([rec(0)], [{"ide": "Code"}]))
    client = TestClient(app)

    forced = client.post("/api/refresh", params={"force": "true"})
    assert forced.status_code == 200
    # Server-Timing names the pool the request waited on, so a slow rebuild is told apart from a queued one.
    assert 'desc="heavy-read"' in forced.headers["server-timing"]
