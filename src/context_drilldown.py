"""Context drill-down: which projects, models, sessions and requests carry the prompt tokens.

The dashboard's prompt-composition card says what prompts are made of overall; this module
answers who drives it. Each covered request contributes either its whole prompt (no label
selected) or its estimated tokens under one composition label. A request without a logged
composition (every Copilot CLI record, older VS Code logs) contributes nothing and shows up
only in the coverage figure, so a share is always "of the part of the period we can see".

Every figure is prompt size times the logged percentage: an estimate, never per-tool billing.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Hashable, List, Optional, Tuple

from src import aggregate, prompt_composition, session_diagnostics
from src.vscode_parser import RequestRecord

TOP_N = 8

ESTIMATE_NOTE = (
    "Estimates: each request's prompt size times the share Copilot logged for it. "
    "Not a per-tool or per-file billing figure."
)


def _pct(part: float, whole: float) -> Optional[float]:
    return round(100.0 * part / whole, 1) if whole else None


def coverage(in_period: List[RequestRecord]) -> Dict[str, Any]:
    """How much of the period carries a prompt composition, by requests and by prompt tokens."""
    covered = [r for r in in_period if prompt_composition.has_composition(r)]
    tokens_total = sum(r.prompt_tokens for r in in_period)
    tokens_covered = sum(r.prompt_tokens for r in covered)
    return {
        "requests_total": len(in_period),
        "requests_covered": len(covered),
        "requests_pct": _pct(len(covered), len(in_period)),
        "tokens_total": tokens_total,
        "tokens_covered": tokens_covered,
        "tokens_pct": _pct(tokens_covered, tokens_total),
    }


def _contribution(r: RequestRecord, label: str) -> float:
    """Tokens this request puts on ``label`` (its whole prompt for the empty label); 0 if uncovered."""
    entries = prompt_composition.label_tokens(r)
    if not entries:
        return 0.0
    if not label:
        return float(r.prompt_tokens)
    return sum(tokens for name, _category, tokens in entries if name == label)


def _group(
    contributions: List[Tuple[RequestRecord, float]],
    key_fn: Callable[[RequestRecord], Hashable],
    describe: Callable[[RequestRecord], Dict[str, Any]],
    total: float,
) -> List[Dict[str, Any]]:
    groups: Dict[Hashable, Dict[str, Any]] = {}
    for r, tokens in contributions:
        g = groups.get(key_fn(r))
        if g is None:
            g = groups[key_fn(r)] = {**describe(r), "est_tokens": 0.0, "requests": 0}
        g["est_tokens"] += tokens
        g["requests"] += 1
    rows = sorted(groups.values(), key=lambda g: (-g["est_tokens"], -g["requests"]))
    for g in rows:
        g["share_pct"] = _pct(g["est_tokens"], total)
        g["est_tokens"] = int(g["est_tokens"])
    return rows[:TOP_N]


def _request_rows(contributions: List[Tuple[RequestRecord, float]], total: float) -> List[Dict[str, Any]]:
    top = sorted(contributions, key=lambda c: (-c[1], c[0].ts, c[0].request_id))[:TOP_N]
    return [
        {
            "request_id": r.request_id,
            "session_id": r.session_id,
            "session_title": r.session_title,
            "ts": r.ts.isoformat(),
            "project": r.project_name,
            "model": r.model,
            "message": r.message,
            "prompt_tokens": r.prompt_tokens,
            "est_tokens": int(tokens),
            "share_pct": _pct(tokens, total),
        }
        for r, tokens in top
    ]


def _empty(period: str, label: str, cov: Dict[str, Any], reason: str, labels: List[dict]) -> Dict[str, Any]:
    return {
        "period": period, "label": label, "state": "no_data", "reason": reason, "coverage": cov,
        "labels": labels, "total_est_tokens": 0,
        "projects": [], "models": [], "sessions": [], "requests": [],
        "note": ESTIMATE_NOTE,
    }


def drilldown(records: List[RequestRecord], period: str, label: str = "") -> Dict[str, Any]:
    """The largest contributors to ``label`` (all prompt tokens when empty) in ``period``."""
    if period not in aggregate.VALID_PERIODS:
        period = "week"
    in_period = aggregate.records_in_period(records, period)
    cov = coverage(in_period)
    labels = aggregate.prompt_mix(in_period)

    if not in_period:
        return _empty(period, label, cov, "No requests in this period.", labels)
    if not cov["requests_covered"]:
        return _empty(
            period, label, cov,
            "None of this period's requests carry a prompt composition "
            "(only VS Code chat logs record one).",
            labels,
        )

    models = aggregate.labels_for(records)
    contributions = [(r, _contribution(r, label)) for r in in_period]
    contributions = [(r, tokens) for r, tokens in contributions if tokens > 0]
    total = sum(tokens for _r, tokens in contributions)
    if not total:
        return _empty(period, label, cov, 'No request in this period carries the label "%s".' % label, labels)

    flags = session_diagnostics.flags_by_session(in_period)
    sessions = _group(
        contributions, lambda r: (r.ide, r.session_id),
        lambda r: {
            "session_id": r.session_id, "title": r.session_title, "project": r.project_name,
            "project_key": r.project_key, "ide": r.ide,
        },
        total,
    )
    for row in sessions:
        row["flags"] = flags.get((row["ide"], row["session_id"]), [])
    return {
        "period": period,
        "label": label,
        "state": "ok",
        "reason": None,
        "coverage": cov,
        "labels": labels,
        "total_est_tokens": int(total),
        "projects": _group(
            contributions, lambda r: r.project_key,
            lambda r: {"project_key": r.project_key, "project": r.project_name}, total,
        ),
        "models": _group(
            contributions, lambda r: models.key(r.model), lambda r: {"model": models.label(r.model)}, total,
        ),
        "sessions": sessions,
        "requests": _request_rows(contributions, total),
        "note": ESTIMATE_NOTE,
    }
