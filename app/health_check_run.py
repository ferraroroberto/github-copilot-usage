"""The checks of the read-model health check, run in a child process over a disposable copy.

``src.health_check`` starts this module with the runtime-data folder and the config file already
pointed at a temporary folder, so every write lands in the copy. It then drives the real API in
process: no socket is opened, discovery is replaced by a replay of the copy's own sources, the billing
and alert blocks are off, and any outbound connection raises. One JSON line per finished check goes to
stdout, then a final ``done`` line, so a run that dies midway still reports what it measured.

A line carries a check's id, its timings and the count of SQL statements by their first keyword; never
a statement, a parameter, a path or any text of the data. Usage: ``python -m app.health_check_run
<pristine copy>``.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src import read_model
from src.health_check import BUDGET_MS, SAMPLES

Parsed = Tuple[list, Dict[str, Any]]


_LOOPBACK = ("127.0.0.1", "::1", "localhost", "")


def _is_loopback(address: Any) -> bool:
    """Whether a socket address stays on this machine (asyncio's own wake-up socket pair does)."""
    host = address[0] if isinstance(address, tuple) and address else address
    return host in _LOOPBACK or host is None


def block_network() -> None:
    """Make every outbound connection raise, so a check can never reach a service."""
    connect, connect_ex, resolve = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def refuse() -> None:
        raise OSError("outbound connections are blocked during the health check")

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if not _is_loopback(address):
            refuse()
        return connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_loopback(address):
            refuse()
        return connect_ex(self, address)

    def guarded_resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(host):
            refuse()
        return resolve(host, *args, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[assignment]
    socket.getaddrinfo = guarded_resolve  # type: ignore[assignment]


class StatementCounter:
    """Counts the SQL statements every new connection runs, by their first keyword."""

    def __init__(self) -> None:
        self.counts: Dict[str, int] = {}
        self._connect = sqlite3.connect

    def install(self) -> None:
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            conn = self._connect(*args, **kwargs)
            conn.set_trace_callback(self._seen)
            return conn

        sqlite3.connect = connect  # type: ignore[assignment]

    def _seen(self, statement: str) -> None:
        words = statement.split(None, 1)
        word = words[0].upper() if words else "OTHER"
        self.counts[word] = self.counts.get(word, 0) + 1

    def take(self) -> Dict[str, int]:
        counts, self.counts = self.counts, {}
        return counts


def _stored_sources(pristine: str) -> Tuple[List[Tuple[str, str]], Set[Tuple[str, int]]]:
    """The copy's sources as (key, signature), and the (key, ord) of every record left out of the totals."""
    conn = read_model.open_file(Path(pristine))
    try:
        sources = [(k, s) for k, s in conn.execute("SELECT key, signature FROM sources ORDER BY id")]
        left_out = {(k, o) for k, o in conn.execute(
            "SELECT s.key, r.ord FROM records r JOIN sources s ON s.id = r.source_id WHERE r.counted = 0"
        )}
        return sources, left_out
    finally:
        conn.close()


def _replay_build(pristine: str) -> Callable[[], Tuple[list, List[dict]]]:
    """A build that serves the records of the pristine copy through the working read model.

    It stands in for discovery: a source the working copy lost (a forced refresh drops them all) is
    read again from the pristine copy, which is what a rebuild from the files costs.
    """
    sources, left_out = _stored_sources(pristine)

    def reread(key: str) -> Parsed:
        conn = read_model.open_file(Path(pristine))
        try:
            row = conn.execute("SELECT id, meta FROM sources WHERE key = ?", (key,)).fetchone()
            records = [read_model.record_from_row(r) for r in conn.execute(read_model.SELECT_RECORDS, (row[0],))]
            return records, json.loads(row[1])
        finally:
            conn.close()

    def visit(scan: "read_model.Scan") -> Tuple[list, List[dict]]:
        records: list = []
        for key, signature in sources:
            got, _meta = scan.records(key, signature, lambda key=key: reread(key))
            for ord_, rec in enumerate(got):
                if (key, ord_) in left_out:
                    scan.uncount(key, ord_)
                else:
                    records.append(rec)
        return records, [{"kind": "copy", "requests": len(records)}]

    return lambda: read_model.model.collect(visit)


class Runner:
    def __init__(self, client: Any, counter: StatementCounter, emit: Callable[[Dict[str, Any]], None]) -> None:
        self.client = client
        self.counter = counter
        self.emit = emit
        self.facts: Dict[str, Any] = {}

    def check(self, check_id: str, send: Callable[[], Any], samples: int = SAMPLES, enforced: bool = True) -> None:
        """Time ``send`` ``samples`` times; a reply other than 200 fails the check."""
        times: List[int] = []
        failure: Optional[str] = None
        self.counter.take()
        shapes: Dict[str, int] = {}
        for index in range(samples):
            started = time.perf_counter()
            try:
                reply = send()
                status = reply.status_code
            except Exception as exc:  # one check failing must not stop the rest
                failure = type(exc).__name__
                break
            times.append(int((time.perf_counter() - started) * 1000))
            if status != 200:
                failure = "HTTP %d" % status
                break
            if index == 0:
                shapes = self.counter.take()
        self.counter.take()
        median = int(statistics.median(times)) if times else None
        if failure is not None:
            state = "error"
        elif not enforced:
            state = "reported"
        else:
            state = "ok" if (median or 0) <= BUDGET_MS else "over_budget"
        self.emit({
            "id": check_id, "state": state, "samples_ms": times, "median_ms": median,
            "enforced": enforced, "statements": shapes, "failure": failure,
        })

    def get(self, path: str, **params: Any) -> Callable[[], Any]:
        return lambda: self.client.get(path, params=params)


def run(pristine: str, emit: Callable[[Dict[str, Any]], None]) -> None:
    block_network()
    counter = StatementCounter()
    counter.install()

    from starlette.testclient import TestClient

    from src import config, refresh

    refresh.store = refresh.SnapshotStore(build=_replay_build(pristine))
    from app.server import app

    client = TestClient(app)
    runner = Runner(client, counter, emit)
    get = runner.get

    runner.check("summary_cold", get("/api/summary", period="all"), samples=1, enforced=False)
    for period in ("all", "today", "week", "month", "cycle"):
        runner.check("summary_" + period, get("/api/summary", period=period))
    runner.check("sessions", get("/api/sessions", period="all"))
    sessions = client.get("/api/sessions", params={"period": "all"}).json().get("sessions") or []
    requests = client.get("/api/requests", params={"period": "all"}).json()
    runner.check("requests", get("/api/requests", period="all"))
    facets = requests.get("facets") or {}
    filters: Dict[str, Any] = {}
    for dim in ("model", "mode", "project"):
        options = facets.get(dim) or []
        if options:
            filters[dim] = options[0]["value"]
    runner.check("requests_filtered", get("/api/requests", period="all", sort="credits", **filters))
    runner.check("requests_paged", get("/api/requests", period="all", page=2, page_size=25))
    if sessions:
        runner.check("session_detail", get("/api/sessions/" + str(sessions[0]["session_id"])))
    runner.check("refresh", lambda: client.post("/api/refresh"))
    credits = config.load()["monthly_credits"]
    runner.check("config_save", lambda: client.post("/api/config", json={"monthly_credits": credits}))
    runner.check("refresh_force", lambda: client.post("/api/refresh", params={"force": "true"}), 1, False)
    runner.check("cache_clear", lambda: client.post("/api/cache/clear"), enforced=False)
    emit({"done": True, "requests": requests.get("period_total"), "sessions": len(sessions)})


def main(argv: List[str]) -> int:
    def emit(line: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(line) + "\n")
        sys.stdout.flush()

    run(argv[1], emit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
