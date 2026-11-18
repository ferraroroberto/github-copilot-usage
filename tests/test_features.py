"""Feature blocks: each one can be switched off and on again with no restart, the choice survives a
restart, and a block whose dependency is missing says why instead of failing quietly."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

import pytest
from starlette.testclient import TestClient

from src import (
    aggregate,
    billing,
    cli_parser,
    config,
    discovery,
    features,
    pace_alert,
    refresh,
)

ROOT = Path(__file__).resolve().parent.parent
# The real gather, captured before any fixture stubs it.
REAL_GATHER = aggregate.gather_records
IDS = ["copilot_cli", "billing_card", "pace_alert", "usage_log"]


@pytest.fixture
def client(monkeypatch, settings_file):
    """The app over an isolated config file and no records."""
    monkeypatch.setattr(aggregate, "gather_records", lambda: ([], []))
    monkeypatch.setattr(refresh, "store", refresh.SnapshotStore())
    from app.server import app

    with TestClient(app) as c:
        yield c


def _put(client, block_id: str, enabled) -> object:
    return client.put("/api/features/" + block_id, json={"enabled": enabled})


def _state(client) -> dict:
    return {b["id"]: b for b in client.get("/api/features").json()["blocks"]}


def _restart(monkeypatch) -> None:
    monkeypatch.setattr(config, "_loaded", False)
    monkeypatch.setattr(config, "_cache", {})


def test_every_block_is_on_by_default(client):
    state = _state(client)
    assert list(state) == IDS
    assert all(b["enabled"] for b in state.values())
    assert client.get("/api/config").json()["features"] == {}


def test_the_switch_is_stored_in_the_machine_config_and_survives_a_restart(client, monkeypatch, settings_file):
    assert _put(client, "billing_card", False).status_code == 200
    assert json.loads(settings_file.read_text(encoding="utf-8"))["features"] == {"billing_card": False}

    _restart(monkeypatch)
    state = _state(client)
    assert state["billing_card"]["enabled"] is False
    assert all(state[i]["enabled"] for i in IDS if i != "billing_card")

    _put(client, "billing_card", True)
    assert json.loads(settings_file.read_text(encoding="utf-8"))["features"] == {}


def test_a_refused_switch_changes_nothing(client):
    assert _put(client, "nope", False).status_code == 404
    assert _put(client, "billing_card", "yes").status_code == 400
    assert client.post("/api/config", json={"features": {"billing_card": "no"}}).status_code == 400
    assert client.post("/api/config", json={"features": ["billing_card"]}).status_code == 400
    assert all(b["enabled"] for b in _state(client).values())


def test_an_older_cli_flag_reads_as_the_cli_block_being_off(client, monkeypatch, settings_file):
    settings_file.write_text(json.dumps({"include_copilot_cli": False, "monthly_credits": 9}), encoding="utf-8")
    _restart(monkeypatch)
    assert _state(client)["copilot_cli"]["enabled"] is False

    client.post("/api/config", json={"monthly_credits": 10})
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "include_copilot_cli" not in on_disk and on_disk["features"] == {"copilot_cli": False}


def test_copilot_cli_off_skips_its_parser_and_on_restores_it(client, monkeypatch, tmp_path):
    calls: List[Path] = []
    monkeypatch.setattr(aggregate, "gather_records", REAL_GATHER)  # run the real gather over a stand-in CLI folder
    monkeypatch.setattr(discovery, "discover_roots", lambda: [])
    monkeypatch.setattr(discovery, "copilot_cli_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_parser, "all_records", lambda d, cached=None: (calls.append(d) or [], {}))

    _put(client, "copilot_cli", False)
    assert REAL_GATHER() == ([], []) and calls == []

    _put(client, "copilot_cli", True)
    _, sources = REAL_GATHER()
    assert calls == [tmp_path] and [s["kind"] for s in sources] == ["cli"]


def test_billing_off_never_calls_github_and_on_restores_it(client, monkeypatch):
    calls: List[int] = []

    async def fake(days: int) -> dict:
        calls.append(days)
        return {"available": True, "daily": []}

    monkeypatch.setattr(billing, "get_daily_credits", fake)
    assert client.get("/api/billing").status_code == 200

    _put(client, "billing_card", False)
    assert client.get("/api/billing").status_code == 404
    assert calls == [14]

    _put(client, "billing_card", True)
    assert client.get("/api/billing").status_code == 200
    assert calls == [14, 14]


def test_pace_alert_off_stops_its_timer_and_on_restarts_it(client):
    from app import server

    assert server._pace_task is not None and not server._pace_task.done()
    first = server._pace_task

    _put(client, "pace_alert", False)
    assert server._pace_task is None
    assert client.post("/api/alerts/test").status_code == 404

    _put(client, "pace_alert", True)
    assert server._pace_task is not None and server._pace_task is not first and not server._pace_task.done()


def test_usage_log_off_refuses_its_calls_and_on_restores_them(client):
    assert client.get("/api/usage/summary").status_code != 404

    _put(client, "usage_log", False)
    assert client.get("/api/usage/summary").status_code == 404
    assert client.post("/api/usage/events", json={}).status_code == 404

    _put(client, "usage_log", True)
    assert client.get("/api/usage/summary").status_code != 404


def test_a_block_that_cannot_run_here_says_why_and_can_still_be_switched_off(client, monkeypatch):
    monkeypatch.setattr(billing, "_read_pat", lambda: "")
    monkeypatch.setattr(pace_alert, "channel_reason", lambda: "not set: add a token")
    monkeypatch.setattr(discovery, "copilot_cli_dir", lambda: None)

    def broken(name: str):
        raise ImportError("needs a newer Python")

    monkeypatch.setattr(features.importlib, "import_module", broken)
    # The registry holds the reasons by reference, so the pace alert one is read through its module.
    monkeypatch.setattr(features, "BLOCKS", tuple(
        features.Block(b.id, b.label, b.covers, pace_alert.channel_reason if b.id == "pace_alert" else b.reason)
        for b in features.BLOCKS))

    state = _state(client)
    assert all(not state[i]["available"] and state[i]["reason"] for i in IDS)
    assert "GITHUB_COPILOT_BILLING_PAT" in state["billing_card"]["reason"]
    assert state["usage_log"]["reason"] == "ImportError: needs a newer Python"
    assert all(state[i]["enabled"] for i in IDS)

    _put(client, "billing_card", False)
    assert _state(client)["billing_card"]["enabled"] is False


def test_settings_has_one_switch_row_per_block():
    html = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
    assert re.findall(r'data-feature="(\w+)"', html) == IDS
    ui = set(re.findall(r'data-feature-ui="(\w+)"', html))
    assert ui and ui <= set(IDS)
