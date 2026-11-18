"""Copilot CLI usage: session-store.db path, events.jsonl fallback, allocation.

Every fixture is synthetic: a throwaway ``~/.copilot`` layout under tmp_path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src import cli_parser

_USAGE_DDL = """
CREATE TABLE assistant_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL, turn_index INTEGER, agent_id TEXT, model TEXT NOT NULL,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    cache_write_tokens INTEGER, total_nano_aiu INTEGER, duration_ms INTEGER,
    created_at TEXT
)"""
_TURNS_DDL = """
CREATE TABLE turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL, user_message TEXT, assistant_response TEXT, timestamp TEXT
)"""
_SESSIONS_DDL = "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)"

SID = "11111111-aaaa-bbbb-cccc-000000000001"


def _copilot_home(tmp_path: Path) -> Path:
    state = tmp_path / ".copilot" / "session-state"
    state.mkdir(parents=True)
    return state


def _write_events(state: Path, sid: str, events: list) -> None:
    sdir = state / sid
    sdir.mkdir()
    (sdir / "workspace.yaml").write_text("cwd: /work/demo-project\n", encoding="utf-8")
    with (sdir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _shutdown(ts: str, metrics: dict) -> dict:
    return {"type": "session.shutdown", "timestamp": ts, "data": {"modelMetrics": metrics}}


def _metrics(inp: int, out: int, read: int, write: int, nano: int) -> dict:
    return {
        "usage": {"inputTokens": inp, "outputTokens": out, "cacheReadTokens": read, "cacheWriteTokens": write},
        "totalNanoAiu": nano,
    }


def _prompt(ts: str, text: str) -> dict:
    return {"type": "user.message", "timestamp": ts, "data": {"content": text}}


def _checkpoint(ts: str, nano: int) -> dict:
    return {"type": "session.usage_checkpoint", "timestamp": ts, "data": {"totalNanoAiu": nano}}


def _make_db(state: Path, usage_rows: list, turns: list = (), ddl: str = _USAGE_DDL) -> None:
    conn = sqlite3.connect(str(state.parent / "session-store.db"))
    conn.execute(ddl)
    conn.execute(_TURNS_DDL)
    conn.execute(_SESSIONS_DDL)
    conn.execute("INSERT INTO sessions VALUES (?, ?)", (SID, "/work/demo-project"))
    for row in usage_rows:
        conn.execute(
            "INSERT INTO assistant_usage_events (session_id, turn_index, agent_id, model, input_tokens,"
            " output_tokens, cache_read_tokens, cache_write_tokens, total_nano_aiu, duration_ms, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
    for t in turns:
        conn.execute("INSERT INTO turns (session_id, turn_index, user_message, timestamp) VALUES (?,?,?,?)", t)
    conn.commit()
    conn.close()


# Two prompts; prompt 0 runs model A and a sub-agent call, prompt 1 runs model B.
_ROWS = [
    (SID, 0, None, "model-a", 1000, 50, 600, 300, 2_000_000_000, 900, "2026-03-01T10:00:05.000Z"),
    (SID, 0, "sub-1", "model-a", 500, 20, 400, 0, 1_000_000_000, 400, "2026-03-01T10:00:09.000Z"),
    (SID, 1, None, "model-b", 800, 70, 0, 700, 500_000_000, 700, "2026-03-01T11:00:03.000Z"),
]
_TURNS = [
    (SID, 0, "first   prompt", "2026-03-01T10:00:00.000Z"),
    (SID, 1, "second prompt", "2026-03-01T11:00:00.000Z"),
]


def test_session_store_gives_per_prompt_records_matching_session_totals(tmp_path):
    state = _copilot_home(tmp_path)
    _make_db(state, _ROWS, _TURNS)
    # The same session's events.jsonl must not be counted a second time.
    _write_events(state, SID, [
        _prompt("2026-03-01T10:00:00Z", "first prompt"),
        _shutdown("2026-03-01T12:00:00Z", {
            "model-a": _metrics(1500, 70, 1000, 300, 3_000_000_000),
            "model-b": _metrics(800, 70, 0, 700, 500_000_000),
        }),
    ])

    records, usage = cli_parser.all_records(state)

    assert usage["usage_source"] == "session-store"
    assert "session-store.db" in usage["usage_note"]
    assert len(records) == 2
    by_model = {r.model: r for r in records}
    a, b = by_model["model-a"], by_model["model-b"]
    assert a.message == "first prompt"
    assert a.ts.hour == 10 and b.ts.hour == 11
    assert all(r.usage_source == "session-store" and not r.usage_estimated for r in records)
    assert a.elapsed_ms == 1300
    # input volume = uncached (1500 - 1000 - 300) + cache-write 300
    assert a.prompt_tokens == 500
    assert a.cache_read_tokens == 1000 and a.cache_write_tokens == 300
    assert sum(r.credits for r in records) == pytest.approx(3.5)
    assert a.project_name == "demo-project"


def test_without_database_falls_back_to_events_and_says_so(tmp_path):
    state = _copilot_home(tmp_path)
    _write_events(state, SID, [
        _prompt("2026-03-01T10:00:00Z", "only prompt"),
        _shutdown("2026-03-01T10:05:00Z", {"model-a": _metrics(1000, 40, 600, 100, 1_500_000_000)}),
    ])

    records, usage = cli_parser.all_records(state)

    assert usage["usage_source"] == "events.jsonl"
    assert "not found" in usage["usage_note"]
    assert len(records) == 1
    r = records[0]
    assert r.usage_source == "events.jsonl" and not r.usage_estimated
    assert r.message == "only prompt"
    assert r.credits == pytest.approx(1.5)
    assert (r.prompt_tokens, r.cache_read_tokens, r.cache_write_tokens) == (400, 600, 100)


def test_database_with_unknown_shape_falls_back(tmp_path):
    state = _copilot_home(tmp_path)
    _make_db(state, [], ddl=_USAGE_DDL.replace("total_nano_aiu", "nano_units"))
    _write_events(state, SID, [_shutdown("2026-03-01T10:05:00Z", {"model-a": _metrics(10, 1, 0, 0, 1_000_000_000)})])

    records, usage = cli_parser.all_records(state)

    assert usage["usage_source"] == "events.jsonl"
    assert "total_nano_aiu" in usage["usage_note"]
    assert [r.usage_source for r in records] == ["events.jsonl"]


def test_session_totals_are_allocated_by_checkpoint_delta_and_marked_estimated(tmp_path):
    state = _copilot_home(tmp_path)
    _write_events(state, SID, [
        _prompt("2026-03-01T10:00:00Z", "p1"),
        _checkpoint("2026-03-01T10:01:00Z", 3_000_000_000),
        _prompt("2026-03-01T11:00:00Z", "p2"),
        _checkpoint("2026-03-01T11:01:00Z", 4_000_000_000),
        _shutdown("2026-03-01T12:00:00Z", {"model-a": _metrics(1001, 11, 400, 200, 4_000_000_000)}),
    ])

    records, _ = cli_parser.all_records(state)

    assert len(records) == 2
    assert all(r.usage_estimated and r.usage_source == "events.jsonl-allocated" for r in records)
    p1, p2 = sorted(records, key=lambda r: r.ts)
    assert (p1.message, p2.message) == ("p1", "p2")
    assert p1.credits == pytest.approx(3.0) and p2.credits == pytest.approx(1.0)
    # Integer splits keep the session totals exact.
    assert p1.cache_read_tokens + p2.cache_read_tokens == 400
    assert p1.cache_write_tokens + p2.cache_write_tokens == 200
    assert p1.completion_tokens + p2.completion_tokens == 11
    assert p1.prompt_tokens + p2.prompt_tokens == 1001 - 400


def test_allocation_without_checkpoints_splits_evenly(tmp_path):
    state = _copilot_home(tmp_path)
    _write_events(state, SID, [
        _prompt("2026-03-01T10:00:00Z", "p1"),
        _prompt("2026-03-01T10:30:00Z", "p2"),
        _shutdown("2026-03-01T11:00:00Z", {"model-a": _metrics(100, 10, 0, 0, 2_000_000_000)}),
    ])

    records, _ = cli_parser.all_records(state)

    assert [round(r.credits, 6) for r in records] == [1.0, 1.0]
    assert [r.prompt_tokens for r in records] == [50, 50]


def test_resumed_session_counts_only_the_last_cumulative_shutdown(tmp_path):
    state = _copilot_home(tmp_path)
    _write_events(state, SID, [
        _shutdown("2026-03-01T10:00:00Z", {"model-a": _metrics(100, 10, 0, 0, 1_000_000_000)}),
        _shutdown("2026-03-01T11:00:00Z", {"model-a": _metrics(250, 30, 0, 0, 2_500_000_000)}),
    ])

    records, _ = cli_parser.all_records(state)

    assert len(records) == 1
    assert records[0].credits == pytest.approx(2.5)


def test_in_flight_session_uses_last_checkpoint_as_estimate(tmp_path):
    state = _copilot_home(tmp_path)
    _write_events(state, SID, [
        {"type": "session.model_change", "timestamp": "2026-03-01T09:59:00Z", "data": {"newModel": "model-c"}},
        _prompt("2026-03-01T10:00:00Z", "p1"),
        _checkpoint("2026-03-01T10:01:00Z", 700_000_000),
    ])

    records, _ = cli_parser.all_records(state)

    assert len(records) == 1
    r = records[0]
    assert r.model == "model-c"
    assert r.usage_estimated and r.usage_source == "events.jsonl-checkpoint"
    assert r.credits == pytest.approx(0.7)


def test_cli_prompt_is_kept_whole_in_both_sources_while_vscode_stays_capped(tmp_path):
    long_text = "word " * 200  # 1,000 characters
    state = _copilot_home(tmp_path)
    _write_events(state, SID, [
        _prompt("2026-03-01T10:00:00Z", long_text),
        _shutdown("2026-03-01T10:05:00Z", {"model-a": _metrics(10, 1, 0, 0, 1_000_000_000)}),
    ])
    events_only, _ = cli_parser.all_records(state)
    assert events_only[0].message == long_text.strip()
    assert len(events_only[0].message) == 999

    from src import vscode_parser
    assert len(vscode_parser._message_text({"message": {"text": long_text}})) == 200

    store_state = _copilot_home(tmp_path / "store")
    _make_db(store_state, _ROWS[:1], [(SID, 0, long_text, "2026-03-01T10:00:00.000Z")])
    from_store, _ = cli_parser.all_records(store_state)
    assert len(from_store[0].message) == 999


def test_naive_timestamp_is_host_local_time_and_aware_keeps_its_instant(monkeypatch):
    from datetime import timedelta, timezone

    monkeypatch.setattr(cli_parser, "_host_tz", lambda: timezone(timedelta(hours=1)))

    naive = cli_parser._parse_ts("2026-01-05T23:30:00")
    assert (naive.hour, naive.minute, naive.day) == (22, 30, 5) and naive.utcoffset() == timedelta(0)
    assert cli_parser._parse_ts("2026-01-05T23:30:00Z").hour == 23
    assert cli_parser._parse_ts("2026-01-05T23:30:00.123Z").hour == 23
    offset = cli_parser._parse_ts("2026-01-05T23:30:00+05:00")
    assert (offset.hour, offset.day) == (18, 5)
    # Space-separated naive values (a local rendering) are local too.
    assert cli_parser._parse_ts("2026-01-05 23:30:00").hour == 22


def test_unparseable_timestamp_falls_back_to_now():
    from datetime import datetime, timedelta, timezone

    got = cli_parser._parse_ts("not a time")
    assert abs(datetime.now(tz=timezone.utc) - got) < timedelta(seconds=5)
    assert abs(datetime.now(tz=timezone.utc) - cli_parser._parse_ts(None)) < timedelta(seconds=5)


def test_replayed_prompt_with_the_same_interaction_id_counts_once(tmp_path):
    state = _copilot_home(tmp_path)

    def prompt(ts, text, interaction):
        ev = _prompt(ts, text)
        if interaction:
            ev["data"]["interactionId"] = interaction
        return ev

    _write_events(state, SID, [
        prompt("2026-03-01T10:00:00Z", "p1", "i-1"),
        prompt("2026-03-01T11:00:00Z", "p2", "i-2"),
        prompt("2026-03-01T11:00:00Z", "p2", "i-2"),  # replayed on resume
        prompt("2026-03-01T12:00:00Z", "no id", None),
        prompt("2026-03-01T12:00:00Z", "no id", None),  # without an id nothing is merged
        _shutdown("2026-03-01T13:00:00Z", {"model-a": _metrics(100, 10, 0, 0, 4_000_000_000)}),
    ])

    records, _ = cli_parser.all_records(state)

    assert sorted(r.message for r in records) == ["no id", "no id", "p1", "p2"]
    assert sum(r.credits for r in records) == pytest.approx(4.0)
