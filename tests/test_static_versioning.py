"""Static assets are cache-busted: stamped URLs, explicit Cache-Control, a shell that revalidates."""

from __future__ import annotations

import re
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from src.static_versioning import CachingStaticFiles, StaticVersion, rewrite_index_html, rewrite_js_imports

LONG = "public, max-age=31536000, immutable"
INDEX = (
    '<link rel="stylesheet" href="styles.css">\n'
    '<link rel="icon" href="favicon.svg">\n'
    '<script src="vendor/lib.min.js"></script>\n'
    '<script src="https://cdn.example/x.js"></script>\n'
    '<script type="module" src="app.js"></script>\n'
)


def _tree(root: Path, app_js: str = "import { a } from './_vendored/mod/a.js';\n") -> Path:
    static = root / "static"
    (static / "_vendored" / "mod").mkdir(parents=True)
    (static / "vendor").mkdir()
    (static / "index.html").write_text(INDEX, encoding="utf-8")
    (static / "styles.css").write_text("body{}", encoding="utf-8")
    (static / "app.js").write_text(app_js, encoding="utf-8")
    (static / "vendor" / "lib.min.js").write_text("var lib=1;", encoding="utf-8")
    (static / "_vendored" / "mod" / "a.js").write_text("import { b } from './b.js';\nexport const a = 1;\n", encoding="utf-8")
    (static / "_vendored" / "mod" / "b.js").write_text("export const b = 2;\n", encoding="utf-8")
    (static / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    (static / "icon.png").write_bytes(b"\x89PNG")
    return static


def _client(static: Path) -> TestClient:
    version = StaticVersion(static)
    app = Starlette(routes=[
        Route("/", lambda request: version.index_response()),
        Mount("/", CachingStaticFiles(directory=static, version=version), name="static"),
    ])
    return TestClient(app)


def _stamp(text: str) -> str:
    found = re.findall(r"\?v=([0-9a-f]{8})", text)
    assert found, text
    assert len(set(found)) == 1, "one fleet hash everywhere"
    return found[0]


def test_js_is_immutable_and_its_imports_carry_the_stamp(tmp_path: Path) -> None:
    static = _tree(tmp_path)
    client = _client(static)
    stamp = _stamp(client.get("/").text)
    response = client.get(f"/app.js?v={stamp}")
    assert response.headers["cache-control"] == LONG
    assert f"from './_vendored/mod/a.js?v={stamp}'" in response.text
    nested = client.get("/_vendored/mod/a.js")
    assert f"from './b.js?v={stamp}'" in nested.text


def test_an_edit_to_any_module_changes_the_stamp(tmp_path: Path) -> None:
    static = _tree(tmp_path)
    before = _stamp(_client(static).get("/").text)
    (static / "_vendored" / "mod" / "b.js").write_text("export const b = 3;\n", encoding="utf-8")
    client = _client(static)
    after = _stamp(client.get("/").text)
    assert after != before
    assert f"?v={after}" in client.get("/app.js").text


def test_shell_revalidates_and_stamps_every_relative_asset(tmp_path: Path) -> None:
    client = _client(_tree(tmp_path))
    response = client.get("/")
    assert response.headers["cache-control"] == "no-cache, must-revalidate"
    stamp = _stamp(response.text)
    for url in ("styles.css", "vendor/lib.min.js", "app.js"):
        assert f'"{url}?v={stamp}"' in response.text
    assert 'href="favicon.svg"' in response.text
    assert 'src="https://cdn.example/x.js"' in response.text
    assert client.get("/index.html").headers["cache-control"] == "no-cache, must-revalidate"


def test_css_icons_and_manifest_get_their_own_policy(tmp_path: Path) -> None:
    client = _client(_tree(tmp_path))
    assert client.get("/styles.css").headers["cache-control"] == LONG
    assert client.get("/manifest.webmanifest").headers["cache-control"] == "public, max-age=86400"
    assert client.get("/icon.png").headers["cache-control"] == "public, max-age=86400"


def test_rewriters_are_idempotent_and_leave_unknown_files_alone() -> None:
    hashes = {"app.js": "abc12345", "x/y.js": "abc12345"}
    once = rewrite_index_html('<script src="app.js?v=old"></script><script src="other.js"></script>', hashes)
    assert once == '<script src="app.js?v=abc12345"></script><script src="other.js"></script>'
    assert rewrite_index_html(once, hashes) == once
    js = rewrite_js_imports("import a from '../app.js';\nimport b from './nope.js';\n", hashes, "x")
    assert js == "import a from '../app.js?v=abc12345';\nimport b from './nope.js';\n"
    assert rewrite_js_imports(js, hashes, "x") == js


def test_a_missing_static_dir_degrades_to_unstamped_urls(tmp_path: Path) -> None:
    version = StaticVersion(tmp_path / "absent")
    assert version.fleet_hash == ""
    assert version.stamp_html(INDEX) == INDEX


def test_the_real_dashboard_shell_and_modules_are_stamped(tmp_path: Path, monkeypatch) -> None:
    # Importing the server resolves the usage-log database folder; keep it out of the real data root.
    monkeypatch.setenv("GITHUB_COPILOT_USAGE_DATA_DIR", str(tmp_path))
    from app import server

    client = TestClient(server.app)
    shell = client.get("/")
    assert shell.headers["cache-control"] == "no-cache, must-revalidate"
    stamp = _stamp(shell.text)
    for url in ("styles.css", "_vendored/page-foot/page-foot.css", "vendor/chart.umd.min.js", "app.js"):
        assert f'"{url}?v={stamp}"' in shell.text
    app_js = client.get(f"/app.js?v={stamp}")
    assert app_js.headers["cache-control"] == LONG
    assert f"./_vendored/usage-log/usage-log.js?v={stamp}" in app_js.text
    assert f"./_vendored/page-foot/page-foot.js?v={stamp}" in app_js.text
