"""The build readout's restart route is mounted and declares only this repo's tray recipe.

`src/self_restart.py` is vendored verbatim and tested in project-scaffolding; this covers the app-side
wiring. No test posts a same-origin restart, because that would run the real `tray.bat --restart`.
"""

from pathlib import Path

from starlette.testclient import TestClient

from app.server import app
from src.self_restart import TRAY_RESTART

ROOT = Path(__file__).resolve().parents[1]


def test_the_app_declares_the_tray_recipe_as_its_only_target():
    res = TestClient(app).get("/api/restart")
    assert res.status_code == 200
    assert [t["id"] for t in res.json()["targets"]] == ["app"]


def test_a_cross_origin_post_is_refused_and_starts_nothing():
    res = TestClient(app).post(
        "/api/restart", json={"target": "app"}, headers={"Origin": "https://elsewhere.example"})
    assert res.status_code == 403


def test_the_recipe_runs_the_repos_own_tray_bat():
    assert TRAY_RESTART[-2:] == (".\\tray.bat", "--restart")
    assert (ROOT / "tray.bat").is_file()
