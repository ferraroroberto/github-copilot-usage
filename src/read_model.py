"""The persisted read model: every parsed request, kept in an indexed SQLite file between runs.

Each source a scan visits (one VS Code chat log, the Copilot CLI's session store, one CLI
``events.jsonl``) has a manifest row holding its key and a signature of what was parsed
(file mtime and size, plus the CLI store's own signature where it feeds the result). A scan
compares each source's current signature with its row: an unchanged source is served from
the file (or from this process's memory of it), a new or changed one is parsed and its
records replaced in one transaction, and the rows of a source the scan no longer visits are
deleted. So a restart with unchanged logs parses nothing.

The file lives in this app's runtime-data folder under its own name, apart from the usage
log's database. Copilot's own files are only ever read by the parsers. A corrupt file or one
written by another schema is moved aside (one ``.bad`` copy is kept) and rebuilt. When the
file cannot be used at all, the scan parses in memory as before and ``status()`` carries the
reason, so the dashboard still serves every record and the refresh reply says why nothing
was kept.

A record the scan leaves out of the totals (a request read again from a copied chat log) is
stored with ``counted = 0``, so the counted rows are exactly the records the scan returned.
Each scan that changes the stored rows writes a new ``scan`` token to ``meta`` in the same
transaction; ``reading(token)`` serves a query only while the file still holds that scan, so
an answer read from the file always matches the snapshot built from the same scan.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, TypeVar

from src import config
from src.runtime_data import runtime_db_path
from src.usage_store import APP
from src.vscode_parser import RequestRecord

_log = logging.getLogger(__name__)

DB_FILENAME = "read_model.sqlite3"
# Bump when the tables change shape; a file with another version is moved aside and rebuilt.
SCHEMA_VERSION = 2
_CONNECT_TIMEOUT_S = 5.0

Parsed = Tuple[List[RequestRecord], Dict[str, Any]]
T = TypeVar("T")

# Record fields stored as JSON text, and the one field that only exists on a read-time copy.
_JSON_FIELDS = ("prompt_details", "context_items")
_BOOL_FIELDS = ("error", "usage_estimated")
_RECORD_FIELDS = [f.name for f in fields(RequestRecord) if f.name != "parsed_title"]
# The field layout is part of the schema: a record that gains a field rebuilds the file.
_LAYOUT = ",".join(_RECORD_FIELDS)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    signature TEXT NOT NULL,
    meta TEXT NOT NULL,
    parsed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    %s,
    ts_epoch REAL NOT NULL,
    local_date TEXT NOT NULL,
    counted INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source_id, ord)
);
CREATE INDEX IF NOT EXISTS records_uncounted ON records(source_id, ord) WHERE counted = 0;
CREATE INDEX IF NOT EXISTS records_local_date ON records(local_date);
CREATE INDEX IF NOT EXISTS records_session ON records(session_id);
CREATE INDEX IF NOT EXISTS records_project ON records(project_key);
CREATE INDEX IF NOT EXISTS records_model ON records(model);
CREATE INDEX IF NOT EXISTS records_mode ON records(mode);
CREATE INDEX IF NOT EXISTS records_credits ON records(credits);
CREATE INDEX IF NOT EXISTS records_ts ON records(ts_epoch);
""" % ",\n    ".join(_RECORD_FIELDS)

_INSERT_RECORD = "INSERT INTO records (source_id, ord, %s, ts_epoch, local_date) VALUES (%s)" % (
    ", ".join(_RECORD_FIELDS), ", ".join("?" * (len(_RECORD_FIELDS) + 4)),
)
_SELECT_RECORDS = "SELECT %s FROM records WHERE source_id = ? ORDER BY ord" % ", ".join(_RECORD_FIELDS)


class ReadModelUnusable(Exception):
    """The file could not be opened, or was rebuilt and still failed."""


def db_path() -> Path:
    """The read model's path in the runtime-data folder; resolving it creates nothing."""
    config.load()  # .env overrides must count before the environment is read
    return runtime_db_path(APP, DB_FILENAME)


def open_file(path: Path, read_only: bool = True) -> sqlite3.Connection:
    """A connection to a copy of the read model (the health check's), never to the live file."""
    if read_only:
        # runtime-write-exempt: opened read-only (mode=ro)
        return sqlite3.connect("%s?mode=ro" % path.resolve().as_uri(), uri=True, timeout=_CONNECT_TIMEOUT_S)
    # runtime-write-exempt: the caller names a file in its own temporary folder
    return sqlite3.connect(str(path), timeout=_CONNECT_TIMEOUT_S)


def backup_live(target: Path) -> None:
    """An online, consistent copy of the live file in ``target``; the live file is opened read-only."""
    src = open_file(db_path())
    dst = open_file(target, read_only=False)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()


def _row(source_id: int, ord_: int, rec: RequestRecord) -> tuple:
    values: List[Any] = [source_id, ord_]
    for name in _RECORD_FIELDS:
        value = getattr(rec, name)
        if name in _JSON_FIELDS:
            value = json.dumps(value, ensure_ascii=False)
        elif name == "ts":
            value = value.isoformat()
        elif name in _BOOL_FIELDS:
            value = int(bool(value))
        values.append(value)
    values.append(rec.ts.timestamp())
    values.append(rec.ts.astimezone().date().isoformat())  # the local date the periods bucket on
    return tuple(values)


def _record(row: tuple) -> RequestRecord:
    kw: Dict[str, Any] = {}
    for name, value in zip(_RECORD_FIELDS, row):
        if name in _JSON_FIELDS:
            value = json.loads(value) if value else []
        elif name == "ts":
            value = datetime.fromisoformat(value)
        elif name in _BOOL_FIELDS:
            value = bool(value)
        kw[name] = value
    return RequestRecord(**kw)


class Scan:
    """One pass over the sources: ``records()`` per source, then ``finish()``."""

    def __init__(self, model: "ReadModel", conn: Optional[sqlite3.Connection]) -> None:
        self._model = model
        self._conn = conn
        self._manifest: Dict[str, Tuple[int, str]] = {}
        if conn is not None:
            self._manifest = {k: (i, s) for i, k, s in conn.execute("SELECT id, key, signature FROM sources")}
        self._seen: Set[str] = set()
        self._ids: Dict[str, int] = {k: i for k, (i, _s) in self._manifest.items()}
        self._uncounted: Set[Tuple[int, int]] = set()
        self.parsed = 0
        self.reused = 0
        self.dropped = 0

    def records(self, key: str, signature: str, parse: Callable[[], Parsed]) -> Parsed:
        """The records and metadata of one source: stored ones while ``signature`` holds, else ``parse()``."""
        self._seen.add(key)
        if self._conn is None:
            self.parsed += 1
            return parse()
        row = self._manifest.get(key)
        if row is not None and row[1] == signature:
            self.reused += 1
            return self._model._remembered(self._conn, key, row[0], signature)
        self.parsed += 1
        records, meta = parse()
        self._store(key, signature, records, meta)
        self._model._memo[key] = (signature, records, meta)
        return records, meta

    def _store(self, key: str, signature: str, records: List[RequestRecord], meta: Dict[str, Any]) -> None:
        conn = self._conn
        assert conn is not None
        with conn:
            conn.execute("DELETE FROM sources WHERE key = ?", (key,))
            cur = conn.execute(
                "INSERT INTO sources (key, signature, meta, parsed_at) VALUES (?, ?, ?, ?)",
                (key, signature, json.dumps(meta, sort_keys=True), time.time()),
            )
            source_id = int(cur.lastrowid)
            self._ids[key] = source_id
            conn.executemany(_INSERT_RECORD, [_row(source_id, i, r) for i, r in enumerate(records)])

    def uncount(self, key: str, ord_: int) -> None:
        """Leave record ``ord_`` of source ``key`` out of the totals (it was returned by ``records``)."""
        source_id = self._ids.get(key)
        if source_id is not None:
            self._uncounted.add((source_id, ord_))

    def finish(self) -> Optional[str]:
        """Delete every source this scan did not visit and store which records count.

        Returns the token of the stored scan, or None when nothing is stored.
        """
        if self._conn is None:
            return None
        conn = self._conn
        gone = [(i,) for k, (i, _s) in self._manifest.items() if k not in self._seen]
        stored = set(conn.execute("SELECT source_id, ord FROM records WHERE counted = 0"))
        token_row = conn.execute("SELECT value FROM meta WHERE key = 'scan'").fetchone()
        token = token_row[0] if token_row else None
        if gone or self.parsed or stored != self._uncounted or token is None:
            token = uuid.uuid4().hex
            with conn:
                conn.executemany("DELETE FROM sources WHERE id = ?", gone)
                conn.execute("UPDATE records SET counted = 1 WHERE counted = 0")
                conn.executemany(
                    "UPDATE records SET counted = 0 WHERE source_id = ? AND ord = ?", sorted(self._uncounted),
                )
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('scan', ?)", (token,))
        self.dropped = len(gone)
        for key in [k for k in self._model._memo if k not in self._seen]:
            del self._model._memo[key]
        return token


class ReadModel:
    def __init__(self, path: Optional[Callable[[], Path]] = None) -> None:
        self._path = path or db_path
        self._lock = threading.Lock()
        # Records of the sources already loaded or parsed in this process, so an unchanged
        # source costs no database read after the first scan: key -> (signature, records, meta).
        self._memo: Dict[str, Tuple[str, List[RequestRecord], Dict[str, Any]]] = {}
        self._memo_for: Optional[Path] = None
        self._drop = False
        self._scans = 0
        self._status: Dict[str, Any] = {"state": "not scanned"}
        self._token: Optional[str] = None

    # -- public ----------------------------------------------------------------

    def collect(self, visit: Callable[[Scan], T]) -> T:
        """Run ``visit`` over one scan and finish it; a file found broken is rebuilt once."""
        with self._lock:
            started = time.monotonic()
            path = self._path()
            if self._memo_for != path:
                self._memo.clear()
                self._memo_for = path
            self._token = None
            try:
                result, scan, self._token = self._persisted(path, visit)
                self._status = {"state": "persisted", "path": str(path)}
            except ReadModelUnusable as exc:
                self._memo.clear()
                _log.warning("⚠️ read model unusable, parsing in memory: %s", exc)
                scan = Scan(self, None)
                result = visit(scan)
                self._status = {"state": "memory", "reason": str(exc)}
            self._scans += 1
            self._status.update(parsed=scan.parsed, reused=scan.reused, dropped=scan.dropped)
            if self._scans == 1 or scan.parsed or scan.dropped:
                _log.info(
                    "ℹ️ read model: %d source(s) parsed, %d reused, %d dropped in %d ms",
                    scan.parsed, scan.reused, scan.dropped, int((time.monotonic() - started) * 1000),
                )
            return result

    def reset(self) -> None:
        """Drop every stored record: the next scan parses every source again."""
        with self._lock:
            self._memo.clear()
            self._drop = True
            self._token = None

    def take_token(self) -> Optional[str]:
        """The token of the last stored scan, once: a build that ran no scan gets None."""
        with self._lock:
            token, self._token = self._token, None
            return token

    @contextmanager
    def reading(self, token: Optional[str]) -> Iterator[Optional[sqlite3.Connection]]:
        """A read-only connection inside one read transaction while the file holds scan ``token``.

        Yields None when there is no token, the file cannot be read, or a newer scan has
        replaced that one; the caller then answers from the snapshot's records instead.
        """
        conn = self._read_connection(token) if token is not None else None
        try:
            yield conn
        finally:
            if conn is not None:
                conn.rollback()
                conn.close()

    def status(self) -> Dict[str, Any]:
        """Whether the last scan kept its records on disk, and how many sources it parsed."""
        with self._lock:
            return dict(self._status)

    # -- internals -------------------------------------------------------------

    def _persisted(self, path: Path, visit: Callable[[Scan], T]) -> Tuple[T, Scan, Optional[str]]:
        for attempt in (1, 2):
            conn = self._open(path)
            try:
                if self._drop:
                    with conn:
                        conn.execute("DELETE FROM sources")
                    self._drop = False
                scan = Scan(self, conn)
                result = visit(scan)
                token = scan.finish()
                return result, scan, token
            except sqlite3.DatabaseError as exc:
                if attempt == 2:
                    raise ReadModelUnusable("rebuilt and still failing: %s" % exc) from exc
                conn.close()
                conn = None  # type: ignore[assignment]
                self._move_aside(path, "%s: %s" % (type(exc).__name__, exc))
            finally:
                if conn is not None:
                    conn.close()
        raise AssertionError("unreachable")

    def _open(self, path: Path) -> sqlite3.Connection:
        """A connection to a valid file of this schema, creating or rebuilding it as needed."""
        try:
            # runtime-write-exempt: path comes from runtime_db_path in db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReadModelUnusable("cannot create %s: %s" % (path.parent, exc)) from exc
        for attempt in (1, 2):
            conn: Optional[sqlite3.Connection] = None
            try:
                # runtime-write-exempt: path comes from runtime_db_path in db_path()
                conn = sqlite3.connect(str(path), timeout=_CONNECT_TIMEOUT_S, check_same_thread=False)
                problem = self._prepare(conn)
                if problem is None:
                    return conn
            except sqlite3.DatabaseError as exc:
                problem = "%s: %s" % (type(exc).__name__, exc)
            except OSError as exc:
                if conn is not None:
                    conn.close()
                raise ReadModelUnusable("cannot open %s: %s" % (path, exc)) from exc
            if conn is not None:
                conn.close()
            if attempt == 2:
                raise ReadModelUnusable("rebuilt and still unusable: %s" % problem)
            self._move_aside(path, problem)
        raise AssertionError("unreachable")

    def _prepare(self, conn: sqlite3.Connection) -> Optional[str]:
        """Set the connection up; the reason the file must be rebuilt, or None when it is fine."""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        fresh = version == 0 and conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        if not fresh:
            if version != SCHEMA_VERSION:
                return "schema version %s, expected %s" % (version, SCHEMA_VERSION)
            layout = conn.execute("SELECT value FROM meta WHERE key = 'layout'").fetchone()
            if layout is None or layout[0] != _LAYOUT:
                return "record layout changed"
            # A full check costs a pass over the file, so it runs once per process.
            if self._scans == 0:
                check = conn.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    return "integrity check: %s" % check
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if fresh:
            with conn:
                conn.executescript(_SCHEMA)
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('layout', ?)", (_LAYOUT,))
                conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        return None

    def _read_connection(self, token: str) -> Optional[sqlite3.Connection]:
        path = self._path()
        if not path.exists():
            return None
        conn: Optional[sqlite3.Connection] = None
        try:
            # runtime-write-exempt: opened read-only (mode=ro) on the path from db_path()
            conn = sqlite3.connect(
                "%s?mode=ro" % path.as_uri(), uri=True, timeout=_CONNECT_TIMEOUT_S,
                isolation_level=None, check_same_thread=False,
            )
            conn.execute("BEGIN")
            row = conn.execute("SELECT value FROM meta WHERE key = 'scan'").fetchone()
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            _log.info("ℹ️ read model not readable, answering from the snapshot: %s", exc)
            return None
        if row is None or row[0] != token:
            conn.rollback()
            conn.close()
            return None
        return conn

    def _move_aside(self, path: Path, reason: str) -> None:
        self._memo.clear()
        aside = path.with_name(path.name + ".bad")
        try:
            # runtime-write-exempt: both paths sit in the runtime-data folder of db_path()
            os.replace(str(path), str(aside))
            for suffix in ("-wal", "-shm"):
                side = path.with_name(path.name + suffix)
                if side.exists():
                    side.unlink()
        except OSError as exc:
            raise ReadModelUnusable("cannot move aside %s (%s): %s" % (path.name, reason, exc)) from exc
        _log.info("ℹ️ read model %s moved aside to %s and rebuilt: %s", path.name, aside.name, reason)

    def _remembered(self, conn: sqlite3.Connection, key: str, source_id: int, signature: str) -> Parsed:
        hit = self._memo.get(key)
        if hit is not None and hit[0] == signature:
            return hit[1], hit[2]
        records = [_record(r) for r in conn.execute(_SELECT_RECORDS, (source_id,))]
        meta_row = conn.execute("SELECT meta FROM sources WHERE id = ?", (source_id,)).fetchone()
        meta = json.loads(meta_row[0]) if meta_row else {}
        self._memo[key] = (signature, records, meta)
        return records, meta


model = ReadModel()
