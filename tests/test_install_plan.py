"""The app's declared install inputs (`.fleet.toml` [install]) match the files the repo ships.

The installer itself is vendored and tested where it comes from; this holds only what is specific here:
every sample exists, every target is ignored by git, and an existing config is topped up without a value
changing."""

from __future__ import annotations

import json
from pathlib import Path

from src import installer

ROOT = Path(__file__).resolve().parent.parent


def test_every_declared_pair_has_a_tracked_sample_and_an_ignored_target() -> None:
    plan = installer.load_plan(ROOT)
    pairs = installer.config_pairs(ROOT, plan)
    assert {t for _, t in pairs} == {"config.json", ".env"}
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for sample, target in pairs:
        assert (ROOT / sample).is_file(), sample
        assert target in ignored, "%s must be gitignored" % target
    assert "config.json.bak-*" in ignored


def test_existing_config_gains_only_missing_keys(tmp_path: Path) -> None:
    sample = tmp_path / "config.example.json"
    sample.write_text((ROOT / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"port": 9999, "features": {"billing": False}}), encoding="utf-8")
    report = installer.Report(what_if=False)
    installer.ensure_config(tmp_path, "config.example.json", "config.json", True, report, "stamp")
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert merged["port"] == 9999 and merged["features"] == {"billing": False}
    assert "monthly_credits" in merged and "session_names" in merged
    assert (tmp_path / "config.json.bak-stamp").is_file()
