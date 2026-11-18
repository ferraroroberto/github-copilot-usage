"""The request explorer and the session list answered by SQL over the read model.

Each function takes a connection from ``read_model.model.reading(token)`` and that token, so
it reads the same scan the snapshot was built from, and returns exactly what the in-memory
path returns (``request_explorer.explore``, ``aggregate.build_sessions``) for those records.
What the dashboard may see is applied in the query: only counted rows, nothing before the
telemetry start date, and a renamed session carries its custom name.

The explorer reads its totals and every facet from one grouped scan, one row per
combination of the facet values, and then fetches only the page. Because a token names
fixed content, the model groups, those combinations and the session rows are kept per
token and settings, so turning a page or the next poll does not scan again.

Where the in-memory path keeps first-seen order (facet ties, a session's model list), the
query uses storage order, ``(source_id, ord)``: the same order within one log, which is
where those ties live.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter, OrderedDict
from dataclasses import replace
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

from src import aggregate, config, request_explorer
from src.model_labels import ModelLabels
from src.read_model import _RECORD_FIELDS, _record
from src.request_explorer import Query
from src.vscode_parser import RequestRecord

T = TypeVar("T")

# A row's place in storage order, as one integer a MIN() can pick a first row by.
_POS = "(source_id * 4294967296 + ord)"
_MODE = "(CASE WHEN mode = '' THEN 'unknown' ELSE mode END)"
_RESULT = "(CASE WHEN error THEN 'error' ELSE 'ok' END)"
_TOKENS = (
    "(CASE WHEN prompt_tokens IS NULL AND completion_tokens IS NULL THEN NULL "
    "ELSE coalesce(prompt_tokens, 0) + coalesce(completion_tokens, 0) END)"
)
_SORT_COLUMNS = {"ts": "ts_epoch", "credits": "credits", "tokens": _TOKENS, "elapsed": "elapsed_ms"}
# Distinct filter and period shapes in use are few; this bounds a long-running process.
_MAX_CACHED = 64

_cache: "OrderedDict[tuple, Any]" = OrderedDict()
_cache_lock = threading.Lock()


def _cached(key: tuple, compute: Callable[[], T]) -> T:
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    value = compute()
    with _cache_lock:
        _cache[key] = value
        while len(_cache) > _MAX_CACHED:
            _cache.popitem(last=False)
    return value


class _Where:
    """A conjunction of SQL conditions with their parameters."""

    def __init__(self) -> None:
        self.parts: List[str] = []
        self.params: List[Any] = []

    def add(self, sql: str, *params: Any) -> None:
        self.parts.append(sql)
        self.params.extend(params)

    def extend(self, other: "_Where") -> "_Where":
        out = _Where()
        out.parts = self.parts + other.parts
        out.params = self.params + other.params
        return out

    def sql(self) -> str:
        return " AND ".join(self.parts) if self.parts else "1"

    def key(self) -> tuple:
        return tuple(self.parts), tuple(self.params)


class _View:
    """The settings a query depends on, and the rows the dashboard may see under them."""

    def __init__(self, token: str) -> None:
        cfg = config.load()
        self.names: Dict[str, str] = cfg.get("session_names") or {}
        self.aliases: Dict[str, str] = cfg.get("model_aliases") or {}
        self.visible = _Where()
        self.visible.add("counted = 1")
        start = aggregate.telemetry_start(cfg)
        if start is not None:
            self.visible.add("local_date >= ?", start.isoformat())
        self.key = (token, json.dumps([self.names, self.aliases], sort_keys=True), self.visible.key())

    def labels(self, conn: sqlite3.Connection) -> Tuple[ModelLabels, Counter]:
        """The model groups of every visible record, as ``aggregate.labels_for`` builds them."""
        counts = _cached(("models",) + self.key, lambda: Counter(dict(conn.execute(
            "SELECT model, count(*) FROM records WHERE %s GROUP BY model" % self.visible.sql(),
            self.visible.params,
        ).fetchall())))
        return ModelLabels(counts, self.aliases), counts

    def named(self, rec: RequestRecord) -> RequestRecord:
        if rec.session_id in self.names:
            return replace(rec, session_title=self.names[rec.session_id], parsed_title=rec.session_title)
        return rec


def _in_period(period: str) -> _Where:
    where = _Where()
    lo, hi = aggregate._period_bounds(aggregate.normalize_period(period, "all"), date.today())
    if lo is not None:
        where.add("local_date >= ?", lo.isoformat())
    if hi is not None:
        where.add("local_date <= ?", hi.isoformat())
    return where


def _in(column: str, values: Sequence[Any]) -> Tuple[str, List[Any]]:
    if not values:
        return "0", []
    return "%s IN (%s)" % (column, ", ".join("?" * len(values))), list(values)


# ---------------------------------------------------------------------------
# Request explorer
# ---------------------------------------------------------------------------


def _other_filters(conn: sqlite3.Connection, q: Query, view: _View) -> _Where:
    """The credit range and the text search: the filters no facet counts."""
    where = _Where()
    if q.min_credits is not None or q.max_credits is not None:
        where.add("credits IS NOT NULL")
    if q.min_credits is not None:
        where.add("credits >= ?", q.min_credits)
    if q.max_credits is not None:
        where.add("credits <= ?", q.max_credits)
    if q.text:
        # Python's lower(), not SQLite's ASCII-only one, so a search matches as before.
        def text_hit(needle: str, message: str, title: str, session_id: str) -> int:
            title = view.names.get(session_id, title)
            return int(needle in message.lower() or needle in title.lower() or needle in session_id.lower())

        conn.create_function("text_hit", 4, text_hit, deterministic=True)
        where.add("text_hit(?, message, session_title, session_id)", q.text.lower())
    return where


# (project, raw model, mode, ide, result, passes the other filters, rows, credits, first position, project name)
Combo = Tuple[str, str, str, str, str, int, int, float, int, str]


def _combos(conn: sqlite3.Connection, view: _View, base: _Where, other: _Where) -> List[Combo]:
    """Every combination of facet values in the period, with its rows and credits."""
    sql = (
        "SELECT project_key, model, {mode}, ide, {result}, ({other}), count(*), TOTAL(credits), "
        "MIN({pos}), project_name FROM records WHERE {base} GROUP BY 1, 2, 3, 4, 5, 6"
    ).format(mode=_MODE, result=_RESULT, other=other.sql(), pos=_POS, base=base.sql())
    return _cached(
        ("combos",) + view.key + base.key() + other.key(),
        lambda: [tuple(r) for r in conn.execute(sql, other.params + base.params)],
    )


def _facets_and_totals(
    combos: List[Combo], q: Query, models: ModelLabels,
) -> Tuple[Dict[str, List[dict]], int, float, int]:
    """The facets, the matching rows and credits, and the period's rows, from the combinations."""
    dims = request_explorer._DIMENSIONS
    selected = {dim: getattr(q, dim) for dim in dims}
    counts: Dict[str, Dict[str, int]] = {dim: {} for dim in dims}
    labels: Dict[str, Dict[str, str]] = {dim: {} for dim in dims}
    first: Dict[str, Dict[str, int]] = {dim: {} for dim in dims}
    total, credits, period_total = 0, 0.0, 0
    for project, raw, mode, ide, result, other_ok, n, combo_credits, pos, project_name in combos:
        period_total += n
        if not other_ok:
            continue
        value = {"project": project, "model": models.key(raw), "mode": mode, "ide": ide, "result": result}
        failed = [dim for dim in dims if selected[dim] and value[dim] != selected[dim]]
        if not failed:
            total += n
            credits += combo_credits
        if len(failed) > 1:
            continue
        # As in the in-memory path: no failure counts toward every facet, one failure only toward its own.
        for dim in (dims if not failed else failed):
            option = value[dim]
            counts[dim][option] = counts[dim].get(option, 0) + n
            if pos < first[dim].get(option, pos + 1):
                first[dim][option] = pos
                labels[dim][option] = (
                    project_name if dim == "project" else models.label(raw) if dim == "model" else option
                )
    ordered: Dict[str, Dict[str, int]] = {}
    for dim in dims:
        # A selected option stays listed even when the other filters leave it with no rows.
        ordered[dim] = {selected[dim]: 0} if selected[dim] else {}
        for option in sorted(counts[dim], key=lambda o: first[dim][o]):
            ordered[dim][option] = counts[dim][option]
    return request_explorer._facets(ordered, labels), total, credits, period_total


def _matching(q: Query, models: ModelLabels, raw_models: Counter) -> _Where:
    """The facet selections as SQL conditions."""
    where = _Where()
    if q.project:
        where.add("project_key = ?", q.project)
    if q.model:
        sql, params = _in("model", sorted(m for m in raw_models if models.key(m) == q.model))
        where.add(sql, *params)
    if q.mode:
        where.add("%s = ?" % _MODE, q.mode)
    if q.ide:
        where.add("ide = ?", q.ide)
    if q.result:
        where.add("%s = ?" % _RESULT, q.result)
    return where


def _order_by(q: Query) -> str:
    way = "DESC" if q.order == "desc" else "ASC"
    if q.sort == "ts":
        # Never missing, so the time index can serve the order.
        return "ts_epoch {w}, request_id {w}, source_id, ord".format(w=way)
    # A missing value sorts last either way, and those rows keep storage order.
    return (
        "({k}) IS NULL, ({k}) {w}, "
        "CASE WHEN ({k}) IS NULL THEN NULL ELSE ts_epoch END {w}, "
        "CASE WHEN ({k}) IS NULL THEN NULL ELSE request_id END {w}, source_id, ord"
    ).format(k=_SORT_COLUMNS[q.sort], w=way)


def explore(conn: sqlite3.Connection, token: str, q: Query) -> Dict[str, Any]:
    """``request_explorer.explore`` over the read model: the page, the totals and the facets."""
    view = _View(token)
    models, raw_models = view.labels(conn)
    if q.model:
        q = replace(q, model=models.key(q.model))

    base = view.visible.extend(_in_period(q.period))
    other = _other_filters(conn, q, view)
    facets, total, credits, period_total = _facets_and_totals(_combos(conn, view, base, other), q, models)
    pages = max((total + q.page_size - 1) // q.page_size, 1)
    page = min(q.page, pages)

    matched = base.extend(other).extend(_matching(q, models, raw_models))
    rows = conn.execute(
        "SELECT %s FROM records WHERE %s ORDER BY %s LIMIT ? OFFSET ?"
        % (", ".join(_RECORD_FIELDS), matched.sql(), _order_by(q)),
        matched.params + [q.page_size, (page - 1) * q.page_size],
    ).fetchall() if total else []
    return {
        "query": {**q.echo(), "page": page},
        "total": total,
        "pages": pages,
        "credits": round(credits, 4),
        "period_total": period_total,
        "rows": [request_explorer._row(view.named(_record(r)), models) for r in rows],
        "facets": facets,
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


_SESSION_TOTALS = """
SELECT ide, session_id,
    count(*), count(credits), TOTAL(error), TOTAL(credits),
    TOTAL(CASE WHEN error THEN credits END),
    SUM(prompt_tokens), SUM(completion_tokens), SUM(cache_read_tokens), SUM(cache_write_tokens),
    TOTAL(prompt_tokens IS NULL AND completion_tokens IS NULL), TOTAL(usage_estimated),
    MIN(ts), MAX(ts)
FROM records WHERE %s GROUP BY ide, session_id
"""
# One MIN() makes SQLite take the other columns from the row it picks: the session's first.
_SESSION_FIRST = """
SELECT ide, session_id, MIN(%s), source, session_title, project_name, project_path
FROM records WHERE %%s GROUP BY ide, session_id
""" % _POS
_SESSION_VALUES = """
SELECT ide, session_id, model, mode, usage_source, MIN(%s)
FROM records WHERE %%s GROUP BY ide, session_id, model, mode, usage_source
""" % _POS


def _first_seen(values: List[Tuple[int, str]]) -> List[str]:
    out: List[str] = []
    for _pos, value in sorted(values):
        if value and value not in out:
            out.append(value)
    return out


def _session_rows(conn: sqlite3.Connection, view: _View, where: _Where) -> List[dict]:
    models, _raw = view.labels(conn)
    totals = {
        (row[0], row[1]): row[2:]
        for row in conn.execute(_SESSION_TOTALS % where.sql(), where.params)
    }
    values: Dict[Tuple[str, str], Dict[str, List[Tuple[int, str]]]] = {}
    for ide, sid, model, mode, usage_source, pos in conn.execute(_SESSION_VALUES % where.sql(), where.params):
        v = values.setdefault((ide, sid), {"models": [], "modes": [], "usage_sources": []})
        v["models"].append((pos, models.label(model)))
        v["modes"].append((pos, mode))
        v["usage_sources"].append((pos, usage_source))

    rows: List[dict] = []
    first = conn.execute(_SESSION_FIRST % where.sql(), where.params).fetchall()
    for ide, sid, _pos, source, title, project, project_path in sorted(first, key=lambda f: f[2]):
        (requests, billed, errors, credits, error_credits, prompt, completion, cache_read, cache_write,
         unknown, estimated, first_ts, last_ts) = totals[(ide, sid)]
        name: Optional[str] = view.names.get(sid)
        v = values[(ide, sid)]
        rows.append(aggregate._finish({
            "session_id": sid,
            "ide": ide,
            "source": source,
            # As ``aggregate.visible`` renames: the parsed title is kept, a blank one falls back to the name.
            "title": name if name is not None else title,
            "parsed_title": (title or name) if name is not None else title,
            "custom_name": name if name is not None and title else None,
            "project": project,
            "project_path": project_path,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "models": _first_seen(v["models"]),
            "modes": _first_seen(v["modes"]),
            "usage_sources": _first_seen(v["usage_sources"]),
            "requests": requests,
            "billed_requests": billed,
            "errors": int(errors),
            "credits": credits,
            "error_credits": error_credits,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "unknown_token_requests": int(unknown),
            "estimated_requests": int(estimated),
        }))
    rows.sort(key=lambda x: (x["credits"], x["last_ts"]), reverse=True)
    return rows


def sessions(conn: sqlite3.Connection, token: str, period: str) -> List[dict]:
    """``aggregate.build_sessions`` over the read model: one row per session in ``period``."""
    view = _View(token)
    where = view.visible.extend(_in_period(period))
    rows = _cached(("sessions",) + view.key + where.key(), lambda: _session_rows(conn, view, where))
    # Callers add fields to a row, so each gets its own copy of the cached rows.
    return [dict(r) for r in rows]
