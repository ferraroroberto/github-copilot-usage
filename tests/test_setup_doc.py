"""docs/setup-and-migration.md names every file and setting the code reads or writes, so a new
config key, environment key or data file cannot land without its migration note."""

from __future__ import annotations

import re
from pathlib import Path

from src import config, pace_alert, usage_store

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "setup-and-migration.md").read_text(encoding="utf-8")


def test_every_config_key_is_named() -> None:
    missing = [k for k in config.DEFAULTS if "`%s`" % k not in DOC]
    assert not missing, "config keys missing from the setup doc: %s" % missing


def test_every_env_key_is_named() -> None:
    keys = re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M)
    keys += ["COPILOT_USAGE_PORT", "GITHUB_COPILOT_USAGE_DATA_DIR", "FLEET_DATA_ROOT"]
    missing = [k for k in keys if "`%s`" % k not in DOC]
    assert not missing, "env keys missing from the setup doc: %s" % missing


def test_every_data_file_is_named() -> None:
    missing = [n for n in (usage_store.DB_FILENAME, pace_alert.STATE_FILENAME) if "`%s`" % n not in DOC]
    assert not missing, "data files missing from the setup doc: %s" % missing
