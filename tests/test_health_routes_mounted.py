"""The app serves the fleet's standard health routes beside its own `/health`."""

from __future__ import annotations

from starlette.testclient import TestClient

from app import build_info
from app.version import VERSION


def test_healthz_and_version_answer_beside_health() -> None:
    from app.server import app

    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.get("/healthz").json() == {"ok": True, "service": "github-copilot-usage"}
    assert client.get("/api/version").json() == {
        "version": VERSION, "git_sha": build_info.GIT_SHA, "built_at": build_info.BUILT_AT,
    }
    assert client.get("/health").status_code == 200
