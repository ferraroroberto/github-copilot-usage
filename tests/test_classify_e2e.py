"""This repo's own `[e2e]` rules in `.fleet.toml`: a later edit that under-routes a real surface fails here."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tomllib")  # the classifier needs Python 3.11+; the app itself runs on 3.9+

from scripts.classify_e2e import classify, load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "path, tier",
    [
        ("app/webapp/static/app.js", "full"),
        ("app/server.py", "full"),
        ("src/aggregate.py", "full"),
        ("docs/install.md", "skip"),
    ],
)
def test_representative_path_routes(path: str, tier: str) -> None:
    assert classify([path], load_config(ROOT / ".fleet.toml")).tier == tier
