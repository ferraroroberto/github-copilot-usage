"""Content-hash cache busting for the dashboard's static files.

An installed iOS PWA heuristic-caches a bare ``StaticFiles`` mount's JS and CSS and
keeps running the old build after a restart. Every ``.js`` / ``.css`` URL therefore
carries a ``?v=<hash>`` stamp, the files are served with an explicit ``Cache-Control``
and the ``index.html`` shell always revalidates.

The front end is an ES-module graph, so a per-file hash would go stale when only a
nested module changes. One **fleet hash** (a SHA-256 over every hashable file's own
hash) is stamped everywhere instead: an edit to any asset rotates every stamp and the
whole small graph is re-fetched together.

The hash is computed once when :class:`StaticVersion` is built at startup; the app
restarts on every code edit, so there is no watcher and no per-request hashing.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import posixpath
import re
from pathlib import Path
from typing import Dict, Iterable, Optional

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

logger = logging.getLogger(__name__)

_HASH_LEN = 8

# Suffixes that get hashed and stamped; icons and the manifest are cached by the mount alone.
_HASHED_SUFFIXES = (".js", ".css")

# `.js` / `.css` are keyed by the fleet hash, so their URL changes on every edit and a
# stale copy can never be served. Icons and the manifest rarely change, but a year of
# staleness would be too much, so they revalidate daily.
_LONG_CACHE = "public, max-age=31536000, immutable"
_DAY_CACHE = "public, max-age=86400"
_IMMUTABLE_SUFFIXES = frozenset({".js", ".css"})
_DAILY_SUFFIXES = frozenset({".webmanifest", ".png", ".ico"})
_SHELL_CACHE = "no-cache, must-revalidate"

# `import ... from './foo.js'` or `'../dir/foo.js'`: captures the quoted relative
# specifier and any existing `?v=` so re-stamping a stamped body is idempotent.
_JS_IMPORT_RE = re.compile(
    r"""(from\s*['"])(\.\.?/(?:[\w\-.]+/)*[\w\-.]+\.js)(\?v=[^'"]*)?(['"])"""
)

# A relative `href` / `src` to a `.css` / `.js` file in index.html. Absolute URLs,
# protocol-relative URLs and root-absolute paths are not static assets of this mount.
_INDEX_ASSET_RE = re.compile(
    r"""(href|src)=(['"])((?![a-zA-Z][a-zA-Z0-9+.\-]*:|/)[\w\-./]+\.(?:css|js))(\?v=[^'"]*)?(['"])"""
)


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:_HASH_LEN]


def _iter_hashable_files(static_dir: Path) -> Iterable[Path]:
    for path in sorted(static_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in _HASHED_SUFFIXES:
            yield path


def compute_asset_hashes(static_dir: Path) -> Dict[str, str]:
    """Return ``{relpath: fleet_hash}`` for every hashable file under ``static_dir``.

    Every value is the same fleet hash; the key is the static-dir-relative posix path so
    the rewriters can resolve subdirectory references. An unreadable file is logged and
    left out; a missing directory or no hashable file gives ``{}``, which degrades to
    unstamped URLs instead of crashing the page.
    """
    if not static_dir.exists():
        return {}
    per_file: Dict[str, str] = {}
    for path in _iter_hashable_files(static_dir):
        try:
            per_file[path.relative_to(static_dir).as_posix()] = _short_hash(path.read_bytes())
        except OSError as exc:
            logger.warning("⚠️ could not hash %s (%s)", path, exc)
    if not per_file:
        return {}
    fleet_input = "\n".join(f"{name}:{per_file[name]}" for name in sorted(per_file)).encode("utf-8")
    fleet_hash = _short_hash(fleet_input)
    return {name: fleet_hash for name in per_file}


def fleet_hash_of(hashes: Dict[str, str]) -> str:
    """The one stamp every asset carries; ``""`` when nothing was hashed."""
    return next(iter(hashes.values()), "")


def _etag_matches(if_none_match: Optional[str], etag: str) -> bool:
    """Weak comparison (RFC 9110 13.1.2): ``W/`` prefixes are ignored, ``*`` matches."""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    for tag in if_none_match.split(","):
        tag = tag.strip()
        if (tag[2:] if tag.startswith("W/") else tag) == etag:
            return True
    return False


def rewrite_index_html(body: str, hashes: Dict[str, str]) -> str:
    """Stamp ``?v=<hash>`` onto every relative ``.css`` / ``.js`` href and src.

    A file with no entry in ``hashes`` passes through unchanged, so a new asset that is
    not hashed yet cannot break the page. An existing ``?v=`` is replaced.
    """
    if not hashes:
        return body

    def _sub(match: "re.Match[str]") -> str:
        attr, quote_open, relpath, _existing, quote_close = match.group(1, 2, 3, 4, 5)
        stamp = hashes.get(posixpath.normpath(relpath))
        if not stamp:
            return match.group(0)
        return f"{attr}={quote_open}{relpath}?v={stamp}{quote_close}"

    return _INDEX_ASSET_RE.sub(_sub, body)


def rewrite_js_imports(body: str, hashes: Dict[str, str], from_dir: str = "") -> str:
    """Stamp ``?v=<hash>`` onto every relative ``import`` in a served module.

    ``from_dir`` is the static-dir-relative posix directory of the file being served ("" at
    the root); it resolves ``./`` and ``../`` specifiers against ``hashes``. An import with
    no entry is left alone and an existing ``?v=`` is replaced.
    """
    if not hashes:
        return body

    def _sub(match: "re.Match[str]") -> str:
        prefix, spec, _existing, quote_close = match.group(1, 2, 3, 4)
        stamp = hashes.get(posixpath.normpath(posixpath.join(from_dir, spec)))
        if not stamp:
            return match.group(0)
        return f"{prefix}{spec}?v={stamp}{quote_close}"

    return _JS_IMPORT_RE.sub(_sub, body)


class StaticVersion:
    """The fleet hash of one static directory, computed once, and the rewriters that use it."""

    def __init__(self, static_dir: Path) -> None:
        self._static_dir = static_dir.resolve()
        self.asset_hashes: Dict[str, str] = compute_asset_hashes(static_dir)
        self.fleet_hash: str = fleet_hash_of(self.asset_hashes)

    def stamp_html(self, html: str) -> str:
        """Stamp the asset URLs in the ``index.html`` shell."""
        return rewrite_index_html(html, self.asset_hashes)

    def stamp_js(self, body: str, source_path: Path) -> str:
        """Stamp the relative ``import`` URLs in the module served from ``source_path``."""
        try:
            rel_parent = source_path.resolve().relative_to(self._static_dir).parent
        except ValueError:
            rel_parent = Path(".")
        from_dir = "" if rel_parent == Path(".") else rel_parent.as_posix()
        return rewrite_js_imports(body, self.asset_hashes, from_dir)

    def index_response(self, if_none_match: Optional[str] = None) -> Response:
        """The stamped ``index.html``, revalidated by ETag.

        ``no-cache`` keeps a cached shell from pointing at old assets; the ETag over the
        stamped body lets that revalidation cost a bodiless 304 when nothing changed.
        """
        html = self.stamp_html((self._static_dir / "index.html").read_text(encoding="utf-8"))
        etag = f'"{_short_hash(html.encode("utf-8"))}"'
        headers = {"Cache-Control": _SHELL_CACHE, "ETag": etag}
        if _etag_matches(if_none_match, etag):
            return Response(status_code=304, headers=headers)
        return Response(html, media_type="text/html", headers=headers)


class CachingStaticFiles(StaticFiles):
    """``StaticFiles`` with a per-suffix ``Cache-Control`` and stamped ES-module imports.

    A bare mount sends only ``ETag`` / ``Last-Modified``, which leaves iOS free to
    heuristic-cache the module graph. Here ``.js`` / ``.css`` are immutable (their URL
    carries the fleet hash), icons and the manifest revalidate daily, and each served
    ``.js`` gets its relative imports stamped with the same hash.
    """

    def __init__(self, *, directory: Path, version: StaticVersion) -> None:
        super().__init__(directory=str(directory))
        self._version = version

    def file_response(
        self,
        full_path: "os.PathLike[str]",
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        path = Path(full_path)
        suffix = path.suffix.lower()

        if suffix == ".js":
            try:
                body = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return super().file_response(full_path, stat_result, scope, status_code)
            media_type, _ = mimetypes.guess_type(str(path))
            return Response(
                content=self._version.stamp_js(body, path),
                status_code=status_code,
                media_type=media_type or "text/javascript",
                headers={"Cache-Control": _LONG_CACHE},
            )

        response = super().file_response(full_path, stat_result, scope, status_code)
        if suffix in _IMMUTABLE_SUFFIXES:
            response.headers["Cache-Control"] = _LONG_CACHE
        elif suffix in _DAILY_SUFFIXES:
            response.headers["Cache-Control"] = _DAY_CACHE
        elif suffix == ".html":
            response.headers["Cache-Control"] = _SHELL_CACHE
        return response
