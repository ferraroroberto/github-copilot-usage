"""docs/setup-and-migration.md names every file and setting the code reads or writes, so a new
config key, environment key or data file cannot land without its migration note.

The data-file list is discovered from source (every module-level ``*_FILENAME = "..."`` constant
under ``src/``), not hand-maintained here: a new constant is picked up automatically, so adding one
without a matching doc row fails this test on its own."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from src import config

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "setup-and-migration.md").read_text(encoding="utf-8")


def _data_filenames() -> dict:
    """Every ``NAME_FILENAME = "literal"`` module-level constant declared under ``src/``."""
    found: dict = {}
    for path in sorted((ROOT / "src").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not (isinstance(target, ast.Name) and target.id.endswith("_FILENAME")):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                found["%s.%s" % (path.stem, target.id)] = node.value.value
    return found


def test_every_config_key_is_named() -> None:
    missing = [k for k in config.DEFAULTS if "`%s`" % k not in DOC]
    assert not missing, "config keys missing from the setup doc: %s" % missing


def test_every_env_key_is_named() -> None:
    keys = re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M)
    keys += ["COPILOT_USAGE_PORT", "GITHUB_COPILOT_USAGE_DATA_DIR", "FLEET_DATA_ROOT"]
    missing = [k for k in keys if "`%s`" % k not in DOC]
    assert not missing, "env keys missing from the setup doc: %s" % missing


def test_every_data_file_is_named() -> None:
    files = _data_filenames()
    missing = ["%s (%s)" % (literal, const) for const, literal in files.items() if "`%s`" % literal not in DOC]
    assert not missing, "data files missing from the setup doc: %s" % missing
