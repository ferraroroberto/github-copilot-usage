"""The persisted read model: a restart parses nothing new, changes re-parse only their source, a broken file is rebuilt.

Every log here is synthetic, written under tmp_path; the read model goes to the per-test runtime-data folder.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import List

import pytest

from src import (
    aggregate,
    cli_parser,
    config,
    discovery,
    read_model,
    refresh,
    vscode_parser,
)
from src.discovery import StorageRoot
from tests.test_cli_parser import (
    _ROWS,
    _TURNS,
    SID,
    _copilot_home,
    _make_db,
    _metrics,
    _prompt,
    _shutdown,
    _write_events,
)
from tests.test_copied_log import _log


def _restart(monkeypatch: pytest.MonkeyPatch) -> read_model.ReadModel:
    """What a new server process starts with: empty parser caches and a read model with no memory."""
    vscode_parser.clear_caches()
    cli_parser.clear_caches()
    model = read_model.ReadModel()
    monkeypatch.setattr(read_model, "model", model)
    return model


@pytest.fixture
def logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> List[Path]:
    """Three chat logs under one storage root, the CLI off; the read model starts empty."""
    root = tmp_path / "Code" / "User"
    files = [_log(root / ("s%d.jsonl" % i), credits=float(i + 1), session="sess-%d" % i) for i in range(3)]
    storage = StorageRoot(ide="Code", user_dir=root, origin="config")
    monkeypatch.setattr(discovery, "discover_roots", lambda: [storage])
    monkeypatch.setattr(
        discovery, "chat_session_files", lambda r: [{"path": f, "workspace_dir": None} for f in files if f.exists()]
    )
    monkeypatch.setattr(discovery, "copilot_cli_dir", lambda: None)
    monkeypatch.setattr(config, "feature_enabled", lambda name: False)
    _restart(monkeypatch)
    return files


def _parsed_paths(monkeypatch: pytest.MonkeyPatch) -> List[Path]:
    seen: List[Path] = []
    real = vscode_parser.parse_session_file

    def spy(path, *a, **kw):
        seen.append(path)
        return real(path, *a, **kw)

    monkeypatch.setattr(vscode_parser, "parse_session_file", spy)
    return seen


def _digest(paths: List[Path]) -> List[str]:
    return [hashlib.sha256(p.read_bytes()).hexdigest() + repr(p.stat().st_mtime) for p in paths]


def test_a_restart_with_unchanged_logs_parses_nothing_and_serves_the_same_summary(logs, monkeypatch, caplog):
    first, _ = aggregate.gather_records()
    assert read_model.model.status()["parsed"] == 3

    model = _restart(monkeypatch)
    parsed = _parsed_paths(monkeypatch)
    with caplog.at_level(logging.INFO, logger="src.read_model"):
        second, _ = aggregate.gather_records()

    assert parsed == []
    assert model.status() == {"state": "persisted", "path": str(read_model.db_path()), "parsed": 0, "reused": 3, "dropped": 0}
    assert "0 source(s) parsed, 3 reused" in caplog.text
    assert second == first
    summaries = [aggregate.get_summary(records, "all") for records in (first, second)]
    for summary in summaries:
        summary.pop("generated_at")
    assert summaries[0] == summaries[1]


def test_touching_one_log_reparses_only_it_and_deleting_one_drops_its_records(logs, monkeypatch):
    aggregate.gather_records()
    _restart(monkeypatch)
    parsed = _parsed_paths(monkeypatch)

    _log(logs[1], credits=9.0, session="sess-1")
    os.utime(logs[1], (1_800_000_000, 1_800_000_000))
    records, _ = aggregate.gather_records()
    assert parsed == [logs[1]]
    assert sorted(r.credits for r in records) == [1.0, 3.0, 9.0]

    logs[2].unlink()
    records, _ = aggregate.gather_records()
    assert sorted(r.credits for r in records) == [1.0, 9.0]
    assert read_model.model.status()["dropped"] == 1
    with closing(sqlite3.connect(str(read_model.db_path()))) as conn, conn:
        assert conn.execute("SELECT count(*) FROM records").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 2


def test_records_carry_their_local_date_for_the_indexes(logs):
    records, _ = aggregate.gather_records()
    with closing(sqlite3.connect(str(read_model.db_path()))) as conn, conn:
        dates = {row[0] for row in conn.execute("SELECT local_date FROM records")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert dates == {records[0].ts.astimezone().date().isoformat()}
    assert {"records_local_date", "records_session", "records_project", "records_model", "records_mode",
            "records_credits", "records_ts"} <= indexes


def test_a_truncated_file_is_moved_aside_and_rebuilt(logs, monkeypatch, caplog):
    first, _ = aggregate.gather_records()
    path = read_model.db_path()
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])

    model = _restart(monkeypatch)
    with caplog.at_level(logging.INFO, logger="src.read_model"):
        second, _ = aggregate.gather_records()

    assert second == first
    assert path.with_name(path.name + ".bad").exists()
    assert "moved aside" in caplog.text
    assert model.status()["state"] == "persisted" and model.status()["parsed"] == 3


def test_a_file_from_another_schema_version_is_moved_aside(logs, monkeypatch):
    aggregate.gather_records()
    path = read_model.db_path()
    with closing(sqlite3.connect(str(path))) as conn, conn:
        conn.execute("PRAGMA user_version = 99")

    model = _restart(monkeypatch)
    records, _ = aggregate.gather_records()
    assert len(records) == 3 and model.status()["parsed"] == 3
    assert path.with_name(path.name + ".bad").exists()


def test_force_refresh_rebuilds_from_scratch_and_never_writes_copilots_files(logs, monkeypatch):
    before = _digest(logs)
    store = refresh.SnapshotStore()
    asyncio.run(store.refresh(False))
    parsed = _parsed_paths(monkeypatch)
    outcome = asyncio.run(store.refresh(True))

    assert sorted(parsed) == sorted(logs)
    assert refresh.describe(outcome)["read_model"]["parsed"] == 3
    assert _digest(logs) == before


def test_an_unwritable_data_folder_parses_in_memory_and_says_why(logs, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("GITHUB_COPILOT_USAGE_DATA_DIR", str(blocker / "sub"))

    records, _ = aggregate.gather_records()

    status = read_model.model.status()
    assert len(records) == 3
    assert status["state"] == "memory" and "cannot create" in status["reason"]


def test_a_cli_store_change_rereads_the_events_sessions(tmp_path, monkeypatch):
    state = _copilot_home(tmp_path)
    other = "22222222-aaaa-bbbb-cccc-000000000002"
    _make_db(state, _ROWS, _TURNS)
    _write_events(state, other, [
        _prompt("2026-03-02T10:00:00Z", "only in events"),
        _shutdown("2026-03-02T12:00:00Z", {"model-a": _metrics(100, 10, 0, 0, 1_000_000_000)}),
    ])
    monkeypatch.setattr(discovery, "discover_roots", lambda: [])
    monkeypatch.setattr(discovery, "copilot_cli_dir", lambda: state)
    monkeypatch.setattr(config, "feature_enabled", lambda name: True)
    _restart(monkeypatch)
    first, sources = aggregate.gather_records()
    assert {r.session_id for r in first} == {SID, other}
    assert sources[0]["usage_source"] == "session-store"

    model = _restart(monkeypatch)
    again, sources_again = aggregate.gather_records()
    assert again == first and sources_again == sources
    assert model.status()["parsed"] == 0

    with closing(sqlite3.connect(str(state.parent / "session-store.db"))) as conn, conn:
        conn.execute("INSERT INTO sessions VALUES (?, ?)", ("33333333-aaaa-bbbb-cccc-000000000003", "/x"))
    os.utime(state.parent / "session-store.db", (1_800_000_000, 1_800_000_000))
    aggregate.gather_records()
    assert model.status()["parsed"] == 2  # the store and the events session it does not cover


def test_records_round_trip_every_field(rec):
    original = rec(3, credits=None, prompt_tokens=None, cache_read_tokens=None, usage_estimated=True, error=True,
                   prompt_details=[{"label": "Messages", "percentageOfPrompt": 10}], context_items=["a.py"])
    row = read_model._row(7, 0, original)
    assert read_model.record_from_row(row[2:-2]) == original
    assert json.loads(row[2 + read_model.RECORD_FIELDS.index("context_items")]) == ["a.py"]
