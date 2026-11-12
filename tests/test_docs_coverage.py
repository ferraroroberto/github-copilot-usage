"""The docs name what the code ships: every `src/` module in docs/layout.md and
every HTTP route in the README's API block, so an adopted feature cannot land
without its documentation."""

from __future__ import annotations

import re
from pathlib import Path

from src.usage_log import usage_router

ROOT = Path(__file__).resolve().parent.parent


def test_every_src_module_is_in_the_layout_tree() -> None:
    layout = (ROOT / "docs" / "layout.md").read_text(encoding="utf-8")
    missing = [p.name for p in sorted((ROOT / "src").glob("*.py")) if p.name != "__init__.py" and p.name not in layout]
    assert not missing, "src modules missing from docs/layout.md: %s" % missing


def test_every_route_is_in_the_readme_api_block(tmp_path: Path) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    server = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    paths = set(re.findall(r'@app\.(?:get|post)\("(/[^"]*)"', server))
    paths |= {route.path for route in usage_router(tmp_path / "usage.sqlite3").routes}
    missing = sorted(p for p in paths if p not in readme)
    assert not missing, "routes missing from README.md: %s" % missing
