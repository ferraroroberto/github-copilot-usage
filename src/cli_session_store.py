"""Read-only reader for the Copilot CLI's local ``session-store.db``.

The CLI keeps a SQLite database next to ``session-state/`` whose
``assistant_usage_events`` table holds one row per model call (sub-agent calls
included, keyed to the user turn that spawned them) with input, output,
cache-read and cache-write tokens and ``total_nano_aiu``. ``input_tokens`` there
is the *total* input: it already includes the cache-read and cache-write
tokens. ``turns`` carries each prompt's timestamp and text.

The schema is undocumented, so the reader checks the columns it needs and
returns ``None`` with a reason on anything unexpected; the caller then falls
back to ``events.jsonl``. The database is opened with ``mode=ro`` (never
written, never left open) and re-read only when the database or its WAL file
changes size or mtime.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

_log = logging.getLogger(__name__)

DB_NAME = "session-store.db"

_USAGE_TABLE = "assistant_usage_events"
_USAGE_COLUMNS = (
    "session_id", "turn_index", "model", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "total_nano_aiu", "created_at",
)
_TURN_COLUMNS = ("session_id", "turn_index", "timestamp", "user_message")
_SESSION_COLUMNS = ("id", "cwd")

# A busy CLI holds the write lock only briefly; waiting longer would stall the
# dashboard refresh, and a miss just means this poll uses the fallback.
_CONNECT_TIMEOUT_S = 1.0

_MESSAGE_LIMIT = 200


@dataclass
class UsageCall:
    """One model call as the CLI recorded it."""

    session_id: str
    turn_index: Optional[int]
    model: str
    input_tokens: int           # total input, including cache-read and cache-write
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    nano_aiu: int
    created_at: str
    duration_ms: Optional[int]


@dataclass
class StoreSnapshot:
    calls: List[UsageCall]
    turns: Dict[Tuple[str, int], Tuple[str, str]] = field(default_factory=dict)  # -> (timestamp, message)
    cwds: Dict[str, str] = field(default_factory=dict)


_cache: Dict[str, Tuple[tuple, Optional[StoreSnapshot], str]] = {}


def _signature(db_path: Path) -> Optional[tuple]:
    sig = []
    for p in (db_path, db_path.with_name(db_path.name + "-wal")):
        try:
            st = p.stat()
        except OSError:
            if p == db_path:
                return None
            sig.append(None)
            continue
        sig.append((st.st_mtime, st.st_size))
    return tuple(sig)


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _message(raw: object) -> str:
    return " ".join(str(raw or "").split())[:_MESSAGE_LIMIT]


def _read(db_path: Path) -> Tuple[Optional[StoreSnapshot], str]:
    # runtime-write-exempt: read-only (mode=ro) connection to Copilot's own database
    conn =sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=_CONNECT_TIMEOUT_S)
    try:
        usage_cols = _columns(conn, _USAGE_TABLE)
        missing = [c for c in _USAGE_COLUMNS if c not in usage_cols]
        if missing:
            return None, "no usable " + _USAGE_TABLE + " table (missing " + ", ".join(missing) + ")"
        duration = "duration_ms" if "duration_ms" in usage_cols else "NULL"
        rows = conn.execute(
            "SELECT " + ", ".join(_USAGE_COLUMNS) + ", " + duration
            + " FROM " + _USAGE_TABLE + " ORDER BY id"
        ).fetchall()
        calls = [
            UsageCall(
                session_id=str(r[0]),
                turn_index=r[1] if isinstance(r[1], int) else None,
                model=str(r[2] or "unknown"),
                input_tokens=_int(r[3]),
                output_tokens=_int(r[4]),
                cache_read_tokens=_int(r[5]),
                cache_write_tokens=_int(r[6]),
                nano_aiu=_int(r[7]),
                created_at=str(r[8] or ""),
                duration_ms=r[9] if isinstance(r[9], int) else None,
            )
            for r in rows
            if r[0]
        ]
        snap = StoreSnapshot(calls=calls)
        if all(c in _columns(conn, "turns") for c in _TURN_COLUMNS):
            for sid, idx, ts, msg in conn.execute(
                "SELECT " + ", ".join(_TURN_COLUMNS) + " FROM turns"
            ):
                if sid and isinstance(idx, int):
                    snap.turns[(str(sid), idx)] = (str(ts or ""), _message(msg))
        if all(c in _columns(conn, "sessions") for c in _SESSION_COLUMNS):
            for sid, cwd in conn.execute("SELECT id, cwd FROM sessions"):
                if sid and cwd:
                    snap.cwds[str(sid)] = str(cwd)
        return snap, "ok"
    finally:
        conn.close()


def read_store(db_path: Path) -> Tuple[Optional[StoreSnapshot], str]:
    """Return ``(snapshot, "ok")``, or ``(None, reason)`` when it cannot be used."""
    sig = _signature(db_path)
    if sig is None:
        return None, "not found"
    key = str(db_path)
    hit = _cache.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1], hit[2]
    try:
        snap, reason = _read(db_path)
    except (sqlite3.Error, OSError, ValueError) as exc:
        snap, reason = None, "unreadable (" + type(exc).__name__ + ")"
    if snap is None:
        _log.info("ℹ️ cli_session_store: %s %s — using events.jsonl", db_path.name, reason)
    _cache[key] = (sig, snap, reason)
    return snap, reason
