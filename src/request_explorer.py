"""Request explorer: filter, sort, page and facet the parsed requests of a period.

Works over the mtime-cached ``RequestRecord`` objects the parsers already hold, so a
query never re-parses a log or rebuilds a payload: it filters the cached records and
builds a row dict only for the page it returns.

Facets are disjunctive: each facet counts the requests that match every *other* active
filter, so the counts say how many rows picking that option would show. With no filter
set they are the plain per-period counts.

Credit range: ``min_credits`` / ``max_credits`` apply to billed requests only (a request
that never got billing data has no credit figure to compare). With neither set,
unbilled requests stay in the result.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from src import aggregate
from src.model_labels import ModelLabels
from src.vscode_parser import RequestRecord

SORTS = ("ts", "credits", "tokens", "elapsed")
ORDERS = ("asc", "desc")
RESULTS = ("ok", "error")
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
# The row carries a short preview of the prompt; the full stored text opens with the session.
PREVIEW_CHARS = 120

# Facet dimension -> (record value, display label) for the options list.
_DIMENSIONS = ("project", "model", "mode", "ide", "result")


@dataclass
class Query:
    """One explorer query, already normalised (see ``Query.build``)."""

    period: str = "week"
    project: str = ""
    model: str = ""
    mode: str = ""
    ide: str = ""
    result: str = ""
    min_credits: Optional[float] = None
    max_credits: Optional[float] = None
    text: str = ""
    sort: str = "ts"
    order: str = "desc"
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    @classmethod
    def build(
        cls,
        period: str = "week",
        project: str = "",
        model: str = "",
        mode: str = "",
        ide: str = "",
        result: str = "",
        min_credits: Optional[float] = None,
        max_credits: Optional[float] = None,
        q: str = "",
        sort: str = "ts",
        order: str = "desc",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> "Query":
        """Clamp every field to its allowed values so a stale URL still gives a valid view."""
        return cls(
            period=aggregate.normalize_period(period, "week"),
            project=project or "",
            model=model or "",
            mode=mode or "",
            ide=ide or "",
            result=result if result in RESULTS else "",
            min_credits=min_credits,
            max_credits=max_credits,
            text=(q or "").strip(),
            sort=sort if sort in SORTS else "ts",
            order=order if order in ORDERS else "desc",
            page=max(int(page), 1),
            page_size=min(max(int(page_size), 1), MAX_PAGE_SIZE),
        )

    def echo(self) -> Dict[str, Any]:
        """The effective query, for the client to mirror into its URL."""
        return {
            "period": self.period, "project": self.project, "model": self.model,
            "mode": self.mode, "ide": self.ide, "result": self.result,
            "min_credits": self.min_credits, "max_credits": self.max_credits,
            "q": self.text, "sort": self.sort, "order": self.order,
            "page": self.page, "page_size": self.page_size,
        }


def _result_of(r: RequestRecord) -> str:
    return "error" if r.error else "ok"


def _values(labels: ModelLabels) -> Dict[str, Callable[[RequestRecord], str]]:
    """Record -> the value a facet filters and counts on; a model counts as its group."""
    return {
        "project": lambda r: r.project_key,
        "model": lambda r: labels.key(r.model),
        "mode": lambda r: r.mode or "unknown",
        "ide": lambda r: r.ide,
        "result": _result_of,
    }


def _selected(q: Query, dim: str) -> str:
    return getattr(q, dim)


def _credit_range_ok(q: Query, r: RequestRecord) -> bool:
    if q.min_credits is None and q.max_credits is None:
        return True
    if r.credits is None:
        return False
    if q.min_credits is not None and r.credits < q.min_credits:
        return False
    return q.max_credits is None or r.credits <= q.max_credits


def _text_ok(needle: str, r: RequestRecord) -> bool:
    if not needle:
        return True
    needle = needle.lower()
    return (
        needle in r.message.lower()
        or needle in r.session_title.lower()
        or needle in r.session_id.lower()
    )


def _failed_filters(q: Query, r: RequestRecord, value: Dict[str, Callable[[RequestRecord], str]]) -> List[str]:
    """The filters ``r`` fails: a facet dimension name, or ``other`` for range and text."""
    failed = [
        dim for dim in _DIMENSIONS
        if _selected(q, dim) and value[dim](r) != _selected(q, dim)
    ]
    if not _credit_range_ok(q, r) or not _text_ok(q.text, r):
        failed.append("other")
    return failed


def _sort_key(sort: str) -> Callable[[RequestRecord], Any]:
    if sort == "credits":
        return lambda r: r.credits
    if sort == "tokens":
        return lambda r: r.prompt_tokens + r.completion_tokens
    if sort == "elapsed":
        return lambda r: r.elapsed_ms
    return lambda r: r.ts


def _ordered(rows: List[RequestRecord], q: Query) -> List[RequestRecord]:
    """Sort by the chosen column; a missing value (unbilled, no timing) sorts last either way."""
    key = _sort_key(q.sort)
    present = [r for r in rows if key(r) is not None]
    missing = [r for r in rows if key(r) is None]
    present.sort(key=lambda r: (key(r), r.ts, r.request_id), reverse=q.order == "desc")
    return present + missing


def _preview(message: str) -> str:
    text = " ".join(message.split())
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1].rstrip() + "…"


def _row(r: RequestRecord, labels: ModelLabels) -> Dict[str, Any]:
    return {
        "request_id": r.request_id,
        "session_id": r.session_id,
        "session_title": r.session_title,
        "ts": r.ts.isoformat(),
        "ide": r.ide,
        "project": r.project_name,
        "mode": r.mode,
        "model": r.model,
        "model_label": labels.label(r.model),
        "message": _preview(r.message),
        "credits": r.credits,
        "usd": round(r.credits * aggregate.USD_PER_CREDIT, 4) if r.credits is not None else None,
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        "elapsed_ms": r.elapsed_ms,
        "error": r.error,
        "usage_estimated": r.usage_estimated,
    }


def _facets(counts: Dict[str, Dict[str, int]], labels: Dict[str, Dict[str, str]]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for dim in _DIMENSIONS:
        options = [
            {"value": value, "label": labels[dim].get(value, value), "count": count}
            for value, count in counts[dim].items()
        ]
        options.sort(key=lambda o: (-o["count"], o["label"].lower()))
        out[dim] = options
    return out


def explore(records: List[RequestRecord], q: Query) -> Dict[str, Any]:
    """Run one query: the page of rows, the match totals and the period's facets."""
    in_period = aggregate.records_in_period(records, q.period)
    models = aggregate.labels_for(records)
    value = _values(models)
    # A bookmarked model may be a raw label or an old group key: resolve it to today's group.
    if q.model:
        q = replace(q, model=models.key(q.model))

    matched: List[RequestRecord] = []
    counts: Dict[str, Dict[str, int]] = {dim: {} for dim in _DIMENSIONS}
    labels: Dict[str, Dict[str, str]] = {dim: {} for dim in _DIMENSIONS}
    # A selected option stays listed even when the other filters leave it with no rows.
    for dim in _DIMENSIONS:
        if _selected(q, dim):
            counts[dim][_selected(q, dim)] = 0

    for r in in_period:
        failed = _failed_filters(q, r, value)
        if len(failed) > 1 or failed == ["other"]:
            continue
        # With no failure the request counts toward every facet; failing only dimension
        # ``d`` it counts toward ``d``'s facet alone (that is the option's "would show" count).
        dims: Tuple[str, ...] = tuple(_DIMENSIONS) if not failed else (failed[0],)
        for dim in dims:
            option = value[dim](r)
            counts[dim][option] = counts[dim].get(option, 0) + 1
            if dim == "project":
                labels[dim].setdefault(option, r.project_name)
            elif dim == "model":
                labels[dim].setdefault(option, models.label(r.model))
            else:
                labels[dim].setdefault(option, option)
        if not failed:
            matched.append(r)

    ordered = _ordered(matched, q)
    total = len(ordered)
    pages = max((total + q.page_size - 1) // q.page_size, 1)
    page = min(q.page, pages)
    start = (page - 1) * q.page_size
    credits = sum(r.credits or 0.0 for r in matched)
    return {
        "query": {**q.echo(), "page": page},
        "total": total,
        "pages": pages,
        "credits": round(credits, 4),
        "period_total": len(in_period),
        "rows": [_row(r, models) for r in ordered[start:start + q.page_size]],
        "facets": _facets(counts, labels),
    }
