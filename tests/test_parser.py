"""Parser tests over synthetic session fixtures shaped like real VS Code
Copilot Chat jsonl (kind-0 snapshot + kind-2 insert + kind-1 field patches)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.vscode_parser import (
    decode_file_uri,
    parse_session_file,
    replay_session,
)


def _fixture_lines(ts_ms: int = 1_783_793_828_699):
    return [
        {"kind": 0, "v": {
            "version": 3,
            "sessionId": "sess-1",
            "customTitle": "Refactor the parser",
            "creationDate": ts_ms,
            "requests": [],
        }},
        {"kind": 2, "k": ["requests"], "i": 0, "v": {
            "requestId": "req-1",
            "timestamp": ts_ms,
            "modelId": "copilot/gpt-5-mini",
            "message": {"text": "what is this project?"},
            "modeInfo": {"kind": "agent", "telemetryModeId": "agent"},
            "agent": {"id": "github.copilot.editsAgent"},
        }},
        {"kind": 1, "k": ["requests", 0, "result"], "v": {
            "timings": {"totalElapsed": 32475},
            "metadata": {"resolvedModel": "gpt-5-mini"},
            "details": "GPT-5 mini • 0.8 credits",
        }},
        {"kind": 1, "k": ["requests", 0, "promptTokens"], "v": 30970},
        {"kind": 1, "k": ["requests", 0, "completionTokens"], "v": 1308},
        {"kind": 1, "k": ["requests", 0, "copilotCredits"], "v": 0.77665},
        {"kind": 1, "k": ["requests", 0, "elapsedMs"], "v": 33184},
        {"kind": 1, "k": ["requests", 0, "promptTokenDetails"], "v": [
            {"category": "System", "label": "System Instructions", "percentageOfPrompt": 51},
            {"category": "System", "label": "Tool Definitions", "percentageOfPrompt": 46},
            {"category": "User Context", "label": "Messages", "percentageOfPrompt": 3},
        ]},
        # A second request that never completed: no tokens, no credits.
        {"kind": 2, "k": ["requests"], "i": 1, "v": {
            "requestId": "req-2",
            "timestamp": ts_ms + 60_000,
            "message": {"text": "never answered"},
        }},
    ]


def _write_fixture(tmp_path, lines):
    path = tmp_path / "sess-1.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")
    return path


def test_replay_reconstructs_requests_and_meta():
    meta, requests = replay_session(_fixture_lines())
    assert meta["sessionId"] == "sess-1"
    assert meta["customTitle"] == "Refactor the parser"
    assert len(requests) == 2
    assert requests[0]["copilotCredits"] == pytest.approx(0.77665)
    assert requests[0]["result"]["metadata"]["resolvedModel"] == "gpt-5-mini"


def test_parse_session_file_extracts_billing_fields(tmp_path):
    path = _write_fixture(tmp_path, _fixture_lines())
    records = parse_session_file(path, "Code", "e:\\proj", "proj", "E:\\proj")

    # The pending req-2 has no usage signal and is skipped.
    assert len(records) == 1
    r = records[0]
    assert r.session_id == "sess-1"
    assert r.session_title == "Refactor the parser"
    assert r.model == "gpt-5-mini"
    assert r.mode == "agent"
    assert r.prompt_tokens == 30970
    assert r.completion_tokens == 1308
    assert r.credits == pytest.approx(0.77665)
    assert r.elapsed_ms == 33184
    assert len(r.prompt_details) == 3
    assert r.ts == datetime.fromtimestamp(1_783_793_828_699 / 1000, tz=timezone.utc)
    assert r.message.startswith("what is this project?")


def test_parse_session_file_mtime_cache(tmp_path):
    path = _write_fixture(tmp_path, _fixture_lines())
    first = parse_session_file(path, "Code", "k", "n", None)
    second = parse_session_file(path, "Code", "k", "n", None)
    assert first is second  # identical object: cache hit


def test_model_falls_back_to_details_string(tmp_path):
    lines = _fixture_lines()
    lines[2]["v"]["metadata"] = {}
    path = _write_fixture(tmp_path, lines)
    records = parse_session_file(path, "Code", "k", "n", None)
    assert records[0].model == "GPT-5 mini"


def test_decode_file_uri_windows_and_posix():
    assert decode_file_uri("file:///e%3A/automation/repo") == "E:\\automation\\repo"
    assert decode_file_uri("file:///home/rob/repo") == "/home/rob/repo"
    assert decode_file_uri("https://example.com/x") is None


def _variable_lines():
    """A request carrying a tool, a file, a prompt file, a pasted snippet and a long attachment name."""
    lines = _fixture_lines()
    lines[1]["v"]["variableData"] = {"variables": [
        {"kind": "tool", "id": "tool-1", "name": "mcp_search_tool", "value": "SECRET TOOL SCHEMA"},
        {"kind": "file", "id": "f1", "name": "  parser.py ", "value": {
            "$mid": 1, "path": "/e:/proj/src/parser.py", "scheme": "file", "content": "FILE BODY"}},
        {"kind": "promptFile", "id": "p1", "value": {"uri": {"path": "/e:/proj/.github/copilot-instructions.md"}}},
        {"kind": "paste", "id": "x1", "value": "line one\nline two PASTED TEXT"},
        {"kind": "generic", "id": "g1", "name": "a" * 400},
        {"kind": "file", "id": "f2", "name": "parser.py"},
        "not-a-dict",
    ]}
    return lines


def test_context_items_are_labels_without_content(tmp_path):
    path = _write_fixture(tmp_path, _variable_lines())
    items = parse_session_file(path, "Code", "k", "n", None)[0].context_items

    assert items == ["mcp_search_tool", "parser.py", "copilot-instructions.md", "a" * 160]
    blob = " ".join(items)
    for content in ("SECRET TOOL SCHEMA", "FILE BODY", "PASTED TEXT"):
        assert content not in blob


def test_context_items_empty_without_variable_data(tmp_path):
    path = _write_fixture(tmp_path, _fixture_lines())
    assert parse_session_file(path, "Code", "k", "n", None)[0].context_items == []


def test_composition_percentages_outside_0_to_100_are_dropped(tmp_path):
    lines = _fixture_lines()
    lines[7]["v"] = [
        {"category": "System", "label": "System Instructions", "percentageOfPrompt": 150},
        {"category": "System", "label": "Tool Definitions", "percentageOfPrompt": -1},
        {"category": "System", "label": "Odd", "percentageOfPrompt": "40"},
        {"category": "User Context", "label": "Messages", "percentageOfPrompt": 0},
        {"category": "User Context", "label": "Attachments", "percentageOfPrompt": 100},
    ]
    path = _write_fixture(tmp_path, lines)
    details = parse_session_file(path, "Code", "k", "n", None)[0].prompt_details
    assert [d["label"] for d in details] == ["Messages", "Attachments"]
