"""A chat log reachable under two folders counts each request once."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from src import aggregate, cli_parser, config, discovery, vscode_parser
from src.discovery import StorageRoot
from tests.test_aggregate import _rec

TS_MS = 1_783_793_828_699


def _log(path: Path, credits: float, request_id: Optional[str] = "req-1", session: str = "sess-1") -> Path:
    request = {"timestamp": TS_MS, "modelId": "copilot/gpt-5-mini", "message": {"text": "hi"}}
    if request_id is not None:
        request["requestId"] = request_id
    lines = [
        {"kind": 0, "v": {"version": 3, "sessionId": session, "requests": []}},
        {"kind": 2, "k": ["requests"], "i": 0, "v": request},
        {"kind": 1, "k": ["requests", 0, "promptTokens"], "v": 100},
        {"kind": 1, "k": ["requests", 0, "completionTokens"], "v": 10},
        {"kind": 1, "k": ["requests", 0, "copilotCredits"], "v": credits},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def _gather(monkeypatch, files: List[Path]):
    """Run the real gather with one storage root per file (all the same editor)."""
    roots = [StorageRoot(ide="Code", user_dir=f.parent, origin="config") for f in files]
    by_dir = {r.user_dir: f for r, f in zip(roots, files)}
    monkeypatch.setattr(discovery, "discover_roots", lambda: roots)
    monkeypatch.setattr(
        discovery, "chat_session_files", lambda r: [{"path": by_dir[r.user_dir], "workspace_dir": None}]
    )
    monkeypatch.setattr(discovery, "copilot_cli_dir", lambda: None)
    monkeypatch.setattr(config, "feature_enabled", lambda name: False)
    return aggregate.gather_records()


def test_copied_log_keeps_the_newer_file_once(tmp_path, monkeypatch):
    older = _log(tmp_path / "a" / "s.jsonl", credits=1.0)
    newer = _log(tmp_path / "b" / "s.jsonl", credits=2.0)
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_500, 1_700_000_500))

    records, sources = _gather(monkeypatch, [older, newer])

    assert [r.credits for r in records] == [2.0]
    assert [(s["requests"], s["duplicates_ignored"]) for s in sources] == [(0, 1), (1, 0)]


def test_equal_mtimes_keep_the_smallest_path(tmp_path, monkeypatch):
    first = _log(tmp_path / "a" / "s.jsonl", credits=1.0)
    second = _log(tmp_path / "b" / "s.jsonl", credits=2.0)
    for p in (first, second):
        os.utime(p, (1_700_000_000, 1_700_000_000))

    for order in ([first, second], [second, first]):
        records, _ = _gather(monkeypatch, order)
        assert [r.credits for r in records] == [1.0]


def test_a_request_without_an_id_is_never_merged(tmp_path, monkeypatch):
    one = _log(tmp_path / "a" / "s.jsonl", credits=1.0, request_id=None)
    two = _log(tmp_path / "b" / "s.jsonl", credits=2.0, request_id=None)

    records, sources = _gather(monkeypatch, [one, two])

    assert len(records) == 2 and [s["duplicates_ignored"] for s in sources] == [0, 0]


def test_a_missing_session_id_or_editor_is_never_merged():
    entries = [
        (_rec(session_id=""), 1.0, "a"),
        (_rec(session_id=""), 2.0, "b"),
        (_rec(ide=""), 1.0, "a"),
        (_rec(ide=""), 2.0, "b"),
    ]
    assert vscode_parser.copied_request_losers(entries) == [False] * 4


def test_other_editors_and_repeats_in_one_file_stay(tmp_path):
    entries = [
        (_rec(ide="Code"), 1.0, "a"),
        (_rec(ide="Cursor"), 2.0, "b"),
        (_rec(ide="Code"), 1.0, "a"),
    ]
    assert vscode_parser.copied_request_losers(entries) == [False] * 3


def test_cli_records_are_untouched(monkeypatch):
    twin = [_rec(source="cli", ide="Copilot CLI"), _rec(source="cli", ide="Copilot CLI")]
    monkeypatch.setattr(discovery, "discover_roots", lambda: [])
    monkeypatch.setattr(discovery, "copilot_cli_dir", lambda: Path("."))
    monkeypatch.setattr(config, "feature_enabled", lambda name: True)
    monkeypatch.setattr(cli_parser, "all_records", lambda d, cached=None: (twin, {}))

    records, sources = aggregate.gather_records()

    assert records == twin and "duplicates_ignored" not in sources[0]
