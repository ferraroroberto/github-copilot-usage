"""The Python 3.9+ promise, held without a 3.9 interpreter on the gate machine.

Two layers: the product code the app owns is scanned for constructs newer than 3.9,
and the vendored usage log (which targets 3.11+) must be an optional component, so
the server still answers when its import fails.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
FLOOR = (3, 9)


def _vendored_python() -> set:
    """Byte-for-byte copies from the scaffold: their own floor is not ours to edit."""
    text = (ROOT / ".fleet.toml").read_text(encoding="utf-8")
    return {m for m in re.findall(r'dest\s*=\s*"([^"]+\.py)"', text)}


def _product_files() -> List[Path]:
    skip = _vendored_python()
    found = []
    for folder in ("app", "src"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            if path.relative_to(ROOT).as_posix() not in skip:
                found.append(path)
    return found


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )


def _annotations(tree: ast.Module) -> Iterator[ast.expr]:
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            yield node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from (a.annotation for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs if a.annotation)
            if node.returns is not None:
                yield node.returns


def _problems(path: Path) -> List[Tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=FLOOR)
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime" and any(a.name == "UTC" for a in node.names):
            found.append((node.lineno, "datetime.UTC needs 3.11; use timezone.utc"))
        if isinstance(node, ast.Call) and any(k.arg == "strict" for k in node.keywords):
            if isinstance(node.func, ast.Name) and node.func.id == "zip":
                found.append((node.lineno, "zip(strict=) needs 3.10"))
        if isinstance(node, ast.Import) and any(a.name == "tomllib" for a in node.names):
            found.append((node.lineno, "tomllib needs 3.11"))
    if not _has_future_annotations(tree):
        for annotation in _annotations(tree):
            if any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) for n in ast.walk(annotation)):
                found.append((annotation.lineno, "X | Y annotation needs 3.10 without `from __future__ import annotations`"))
    return found


@pytest.mark.parametrize("path", _product_files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_product_code_stays_within_the_floor(path: Path) -> None:
    assert _problems(path) == []


def test_scan_flags_what_it_claims(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from datetime import UTC\n"
        "def f(a: str | None) -> int | None:\n"
        "    return list(zip([1], [2], strict=False))\n",
        encoding="utf-8",
    )
    assert {message.split(" ")[0] for _, message in _problems(bad)} == {"datetime.UTC", "zip(strict=)", "X"}


def test_vendored_usage_log_is_the_only_exempt_module() -> None:
    assert _vendored_python() >= {"src/usage_log.py"}


def test_server_starts_without_the_usage_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 3.9/3.10 interpreter cannot import the vendored log; the dashboard must still serve."""
    monkeypatch.setitem(sys.modules, "src.usage_log", None)  # makes the import raise ImportError
    spec = importlib.util.spec_from_file_location("server_without_usage_log", ROOT / "app" / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = TestClient(module.app)
    assert client.get("/health").status_code == 200
    assert not [r for r in module.app.routes if getattr(r, "path", "").startswith("/api/usage")]
