"""The usage database path: user-level by default, overridable, and never created on resolve."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from src import usage_store


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_COPILOT_USAGE_DATA_DIR", "FLEET_DATA_ROOT"):
        monkeypatch.delenv(name, raising=False)


def test_default_is_the_users_data_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert usage_store.usage_db_path() == tmp_path / "github-copilot-usage" / "usage.sqlite3"


def test_app_dir_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_COPILOT_USAGE_DATA_DIR", str(tmp_path / "mine"))
    assert usage_store.usage_db_path() == tmp_path / "mine" / "usage.sqlite3"


def test_fleet_root_override_adds_the_app_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_DATA_ROOT", str(tmp_path))
    assert usage_store.usage_db_path() == tmp_path / "github-copilot-usage" / "usage.sqlite3"


def test_resolving_creates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "absent"
    monkeypatch.setenv("GITHUB_COPILOT_USAGE_DATA_DIR", str(target))
    usage_store.usage_db_path()
    assert not target.exists()


def test_importing_the_server_survives_an_uncreatable_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("GITHUB_COPILOT_USAGE_DATA_DIR", str(blocker / "sub"))
    from app import server

    importlib.reload(server)
