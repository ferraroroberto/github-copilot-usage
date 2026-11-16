"""The snapshot of parsed records, how it is refreshed, and the summary cached on top of it.

Every read joins one shared refresh: the work runs as a task the store owns, so a request
that is cancelled while waiting (a closed tab, a phone that lost signal) does not abort the
rebuild for the others. A refresh asked for while another is in flight joins it, except a
force refresh asked for during an incremental one, which needs a full rebuild of its own.
Each refresh takes a sequence number when it starts and only installs its snapshot if no
newer refresh has installed one, so a slow older build never overwrites newer data.

The cached summary is keyed by a fingerprint of the record *content*, not of its count or
newest timestamp, so an edit to one record's credits invalidates it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.vscode_parser import RequestRecord

_log = logging.getLogger(__name__)

Build = Callable[[], Tuple[List[RequestRecord], List[dict]]]

# One period per summary key, a few config shapes and a rolled-over day: far below this in use.
_MAX_SUMMARIES = 64


def fingerprint(records: List[RequestRecord]) -> str:
    """A digest of every field of every record, so any content change gives a new value."""
    digest = hashlib.blake2b(digest_size=16)
    for r in records:
        digest.update(repr(r).encode("utf-8", "replace"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class Snapshot:
    records: List[RequestRecord]
    sources: List[dict]
    generation: int      # the sequence number of the refresh that built it
    fingerprint: str
    built_at: float      # unix seconds
    duration_ms: int
    forced: bool


@dataclass(frozen=True)
class Outcome:
    """What one refresh call did, for the toast: the snapshot, and whether it joined another."""

    snapshot: Snapshot
    joined: bool


def _clear_parser_caches() -> None:
    # Imported here so a test that swaps the builder never needs the parsers loaded.
    from src import cli_parser, vscode_parser

    vscode_parser.clear_caches()
    cli_parser.clear_caches()


class SnapshotStore:
    def __init__(self, build: Optional[Build] = None, clear_caches: Optional[Callable[[], None]] = None) -> None:
        self._build = build
        self._clear_caches = clear_caches or _clear_parser_caches
        # Builds touch the parsers' unlocked dicts, so two never overlap even when a force
        # refresh starts while an incremental one is still running in its thread.
        self._build_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._seq = 0
        self._installed_seq = 0
        self._snapshot: Optional[Snapshot] = None
        self._inflight: Optional["asyncio.Future[Snapshot]"] = None
        self._inflight_forced = False
        self._summaries: Dict[tuple, dict] = {}
        self._summaries_for = ""

    def _gather(self) -> Tuple[List[RequestRecord], List[dict]]:
        if self._build is not None:
            return self._build()
        # Resolved per call so a patched gather_records is honoured.
        from src import aggregate

        return aggregate.gather_records()

    # -- refreshing --------------------------------------------------------------

    def _run_build(self, seq: int, force: bool) -> Snapshot:
        with self._build_lock:
            started = time.monotonic()
            if force:
                self._clear_caches()
            records, sources = self._gather()
            snap = Snapshot(
                records=records,
                sources=sources,
                generation=seq,
                fingerprint=fingerprint(records),
                built_at=time.time(),
                duration_ms=int((time.monotonic() - started) * 1000),
                forced=force,
            )
        return snap

    def _install(self, snap: Snapshot) -> Snapshot:
        with self._state_lock:
            if snap.generation > self._installed_seq:
                self._installed_seq = snap.generation
                self._snapshot = snap
                return snap
            _log.info("ℹ️ refresh %d finished after a newer one installed; its result is dropped", snap.generation)
            return self._snapshot or snap

    async def _work(self, seq: int, force: bool) -> Snapshot:
        snap = await asyncio.get_running_loop().run_in_executor(None, self._run_build, seq, force)
        return self._install(snap)

    async def refresh(self, force: bool = False) -> Outcome:
        """Rebuild the snapshot, or join the rebuild already running.

        Awaiting is shielded: cancelling the caller leaves the shared task running.
        """
        with self._state_lock:
            flight = self._inflight
            if flight is not None and not flight.done() and (self._inflight_forced or not force):
                joined = True
            else:
                joined = False
                self._seq += 1
                flight = asyncio.ensure_future(self._work(self._seq, force))
                self._inflight, self._inflight_forced = flight, force
                flight.add_done_callback(self._finished)
        snap = await asyncio.shield(flight)
        return Outcome(snap, joined)

    def _finished(self, flight: "asyncio.Future[Snapshot]") -> None:
        with self._state_lock:
            if self._inflight is flight:
                self._inflight = None
        if not flight.cancelled() and flight.exception() is not None:
            _log.warning("⚠️ refresh failed: %s", flight.exception())

    async def snapshot(self) -> Snapshot:
        """The data every read endpoint serves: an incremental refresh joined with any other."""
        return (await self.refresh(False)).snapshot

    async def clear(self) -> None:
        """Forget the snapshot, the summaries and the parsers' caches; the next read rebuilds.

        An older build still running is dropped when it finishes, because clearing takes the
        newest sequence number.
        """
        with self._state_lock:
            self._seq += 1
            self._installed_seq = self._seq
            self._snapshot = None
            self._summaries.clear()

        def _wipe() -> None:
            with self._build_lock:
                self._clear_caches()

        await asyncio.get_running_loop().run_in_executor(None, _wipe)

    # -- cached summary ----------------------------------------------------------

    def summary(self, snap: Snapshot, period: str, compute: Callable[[List[RequestRecord], str], dict]) -> dict:
        """``compute(records, period)``, reused while the records, config and local day are unchanged."""
        from src import config

        key = (period, date.today().isoformat(), json.dumps(config.load(), sort_keys=True, default=str))
        with self._state_lock:
            if self._summaries_for != snap.fingerprint:
                self._summaries.clear()
                self._summaries_for = snap.fingerprint
            hit = self._summaries.get(key)
        if hit is None:
            hit = compute(snap.records, period)
            with self._state_lock:
                if self._summaries_for == snap.fingerprint:
                    if len(self._summaries) >= _MAX_SUMMARIES:
                        self._summaries.clear()
                    self._summaries[key] = hit
        return dict(hit)


def describe(outcome: Outcome) -> Dict[str, Any]:
    """The JSON the refresh endpoint returns."""
    snap = outcome.snapshot
    return {
        "ok": True,
        "forced": snap.forced,
        "joined": outcome.joined,
        "generation": snap.generation,
        "requests": len(snap.records),
        "sources": len(snap.sources),
        "built_at": snap.built_at,
        "duration_ms": snap.duration_ms,
    }


store = SnapshotStore()
