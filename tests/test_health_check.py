"""The read-model health check: every check is timed over a copy, the live file is left byte-identical,
a second run is refused while one runs, and the result holds no text of the data."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable, List

import pytest
from starlette.testclient import TestClient

from src import config, health_check, read_model
from src.vscode_parser import RequestRecord

SECRET = "TOPSECRETPROMPT"
EXPECTED = {
    "summary_cold", "summary_all", "summary_today", "summary_week", "summary_month", "summary_cycle",
    "sessions", "requests", "requests_filtered", "requests_paged", "session_detail",
    "refresh", "config_save", "refresh_force", "cache_clear",
}


@pytest.fixture
def live(rec: Callable[..., RequestRecord], settings_file: Path) -> Path:
    """A real read model of synthetic requests whose text carries a marker the result must not hold."""
    records: List[RequestRecord] = [
        rec(n, message="%s %d" % (SECRET, n), session_title=SECRET + " title", days_ago=n % 5,
            project_path="E:\\%s\\path" % SECRET, session_id="s%d" % (n % 4))
        for n in range(40)
    ]
    uncounted = [rec(99, message=SECRET + " copy")]

    def visit(scan: read_model.Scan) -> None:
        scan.records("vscode:a", "1", lambda: (list(records), {}))
        scan.records("vscode:copy", "1", lambda: (list(uncounted), {}))
        scan.uncount("vscode:copy", 0)

    model = read_model.ReadModel()
    model.collect(visit)
    return read_model.db_path()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_run_reports_every_check_and_leaves_the_live_file_alone(live: Path) -> None:
    before = _digest(live)
    config_before = config.CONFIG_PATH.exists()
    result = health_check.run()
    assert result["state"] == "measured", result
    assert {c["id"] for c in result["checks"]} == EXPECTED
    for check in result["checks"]:
        assert check["state"] in ("ok", "reported"), check
        assert check["samples_ms"], check
    by_id = {c["id"]: c for c in result["checks"]}
    assert len(by_id["requests"]["samples_ms"]) == health_check.SAMPLES
    assert len(by_id["summary_cold"]["samples_ms"]) == 1 and not by_id["summary_cold"]["enforced"]
    assert by_id["requests"]["statements"].get("SELECT", 0) > 0, "the read model should have answered"
    assert result["verdict"] == "pass"
    assert result["dataset"]["requests"] == 41 and result["dataset"]["sources"] == 2
    assert _digest(live) == before
    assert config.CONFIG_PATH.exists() == config_before


def test_result_holds_no_text_of_the_data(live: Path, tmp_path: Path) -> None:
    result = health_check.run()
    text = json.dumps(result)
    assert SECRET not in text and "prompt number" not in text
    assert str(tmp_path) not in text and str(live.parent) not in text


def test_unavailable_without_a_read_model(settings_file: Path) -> None:
    result = health_check.run()
    assert result["state"] == "unavailable" and result["verdict"] is None and result["checks"] == []


def test_a_stopped_run_is_incomplete_not_a_pass(live: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_check, "TIMEOUT_S", 0.001)
    result = health_check.run()
    assert result["state"] == "incomplete" and result["verdict"] is None and result["reason"]


def test_second_run_gets_409_and_status_shows_the_last_result(
    live: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.server import app

    gate = {"release": False}
    seen = {}

    def slow_run() -> dict:
        while not gate["release"]:
            time.sleep(0.01)
        return health_check._result("unavailable", reason="stub")

    monkeypatch.setattr(health_check, "run", slow_run)
    monkeypatch.setattr(health_check, "_running", False)
    monkeypatch.setattr(health_check, "_last", None)
    client = TestClient(app)
    first = client.post("/api/health-check")
    assert first.status_code == 202 and first.json()["running"] is True
    assert client.post("/api/health-check").status_code == 409
    assert client.get("/api/health-check").json()["running"] is True
    gate["release"] = True
    for _ in range(200):
        seen = client.get("/api/health-check").json()
        if not seen["running"]:
            break
        time.sleep(0.01)
    assert seen["running"] is False and seen["result"]["reason"] == "stub"
    assert json.loads(health_check.result_path().read_text(encoding="utf-8"))["reason"] == "stub"


def test_a_run_cannot_reach_beyond_loopback() -> None:
    import subprocess
    import sys

    code = (
        "import socket\n"
        "from app.health_check_run import block_network\n"
        "block_network()\n"
        "for call in (lambda: socket.create_connection(('192.0.2.1', 80), 1), lambda: socket.getaddrinfo('example.com', 80)):\n"
        "    try:\n        call()\n    except OSError as exc:\n        assert 'blocked' in str(exc), exc\n    else:\n        raise SystemExit('reached out')\n"
    )
    root = Path(__file__).resolve().parent.parent
    no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=str(root), capture_output=True, timeout=30, creationflags=no_window,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
