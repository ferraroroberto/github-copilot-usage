"""Deterministic, local insights over a period's usage.

Four detectors (top contributors, period-over-period change, budget risk, prompt
composition) read the same figures the dashboard cards show. No model call, no network.
Each insight has a stable ``type``, a ``severity`` (``info`` | ``warning`` | ``critical``),
one short sentence and structured ``evidence`` (period, model, project, session, label)
the UI turns into a link to the matching view. A detector that lacks the data says nothing
rather than guess; a period with no or too few requests says so with ``no_data`` or
``insufficient_evidence``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from src import aggregate, context_drilldown
from src.vscode_parser import RequestRecord

TYPES = (
    "top_contributor", "period_change", "budget_risk", "prompt_composition",
    "no_data", "insufficient_evidence",
)
SEVERITIES = ("critical", "warning", "info")

# Below this many requests no share or trend is worth stating.
MIN_REQUESTS = 5
# Billed credits needed before a share of them means anything.
MIN_CREDITS = 1.0
# One project or model past this share of the period's credits is "what drives it".
TOP_SHARE_PCT = 50.0
TOP_SESSION_SHARE_PCT = 30.0
# A change smaller than this is ordinary variation between two periods.
CHANGE_PCT = 50.0
MIN_PREV_CREDITS = 2.0
COMPOSITION_MIN_PCT = 40.0
COMPOSITION_MIN_COVERED = 3
MAX_COMPOSITION_INSIGHTS = 2

_PREVIOUS = {"today": "yesterday", "week": "the 7 days before", "month": "the 30 days before"}


def _insight(kind: str, severity: str, text: str, **evidence: Any) -> Dict[str, Any]:
    return {
        "type": kind,
        "severity": severity,
        "text": text,
        "evidence": {k: v for k, v in evidence.items() if v is not None},
    }


def _credits(n: float) -> str:
    return "{:,.0f}".format(n) if n >= 100 else "{:,.1f}".format(n).rstrip("0").rstrip(".")


def _top_contributors(summary: Dict[str, Any], period: str) -> List[Dict[str, Any]]:
    totals = summary["totals"]
    if totals["requests"] < MIN_REQUESTS or totals["credits"] < MIN_CREDITS:
        return []
    out: List[Dict[str, Any]] = []
    credits = totals["credits"]

    models = summary["by_model"]
    if len(models) > 1 and models[0]["credits_share_pct"] >= TOP_SHARE_PCT:
        m = models[0]
        out.append(_insight(
            "top_contributor", "info",
            "%s used %s%% of this period's credits (%s of %s)."
            % (m["model"], m["credits_share_pct"], _credits(m["credits"]), _credits(credits)),
            period=period, model=m["model"],
        ))

    projects = summary["by_project"]
    if len(projects) > 1:
        p = projects[0]
        share = round(100.0 * p["credits"] / credits, 1)
        if share >= TOP_SHARE_PCT:
            out.append(_insight(
                "top_contributor", "info",
                "%s used %s%% of this period's credits (%s of %s)."
                % (p["project"], share, _credits(p["credits"]), _credits(credits)),
                period=period, project=p["project_key"],
            ))

    sessions = summary["top_sessions"]
    if len(sessions) > 1:
        s = sessions[0]
        share = round(100.0 * s["credits"] / credits, 1)
        if share >= TOP_SESSION_SHARE_PCT:
            out.append(_insight(
                "top_contributor", "info",
                'One session, "%s", used %s%% of this period\'s credits.' % (s["title"], share),
                period=period, session=s["session_id"],
            ))
    return out


def _period_change(summary: Dict[str, Any], period: str) -> List[Dict[str, Any]]:
    prev = summary["prev_totals"]
    if prev is None or prev["credits"] < MIN_PREV_CREDITS:
        return []
    now = summary["totals"]["credits"]
    change = 100.0 * (now - prev["credits"]) / prev["credits"]
    if abs(change) < CHANGE_PCT:
        return []
    text = "Credits are %s %d%% on %s (%s vs %s)." % (
        "up" if change > 0 else "down", round(abs(change)), _PREVIOUS.get(period, "the span before"),
        _credits(now), _credits(prev["credits"]),
    )
    return [_insight("period_change", "warning" if change > 0 else "info", text, period=period)]


def _budget_risk(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    b = summary["budget"]
    risk = b["risk"]
    used = _credits(b["used_credits"])
    allowance = _credits(b["allowance_credits"])
    if risk == "critical":
        return [_insight(
            "budget_risk", "critical",
            "The cycle allowance is used up: %s of %s credits." % (used, allowance),
            period="cycle",
        )]
    if risk == "warning":
        return [_insight(
            "budget_risk", "warning",
            "On pace for %s credits by cycle end, %s%% of the %s allowance."
            % (_credits(b["projected_credits"]), b["projected_pct"], allowance),
            period="cycle",
        )]
    if risk == "attention":
        return [_insight(
            "budget_risk", "info",
            "%s%% of the cycle allowance is used: %s of %s credits." % (round(b["used_pct"]), used, allowance),
            period="cycle",
        )]
    return []


def _prompt_composition(
    summary: Dict[str, Any], cov: Dict[str, Any], period: str,
) -> List[Dict[str, Any]]:
    if cov["requests_covered"] < COMPOSITION_MIN_COVERED:
        return []
    out: List[Dict[str, Any]] = []
    for row in summary["prompt_mix"]:
        if row["pct"] < COMPOSITION_MIN_PCT or len(out) >= MAX_COMPOSITION_INSIGHTS:
            continue
        is_tools = "tool" in row["label"].lower()
        text = "%s is about %s%% of prompt tokens (%s%% of this period's tokens carry a breakdown)." % (
            row["label"], row["pct"], round(cov["tokens_pct"] or 0),
        )
        if is_tools:
            text += " Disabling unused tools and MCP servers shrinks every request."
        out.append(_insight(
            "prompt_composition", "warning" if is_tools else "info", text,
            period=period, label=row["label"],
        ))
    return out


def build_insights(records: List[RequestRecord], period: str = "week") -> Dict[str, Any]:
    """All insights for ``period``, most severe first, with the state of the evidence."""
    period = aggregate.normalize_period(period, "week")
    summary = aggregate.get_summary(records, period)
    in_period = aggregate.records_in_period(records, period)
    cov = context_drilldown.coverage(in_period)
    requests = summary["totals"]["requests"]

    found: List[Dict[str, Any]] = []
    found += _budget_risk(summary)
    found += _top_contributors(summary, period)
    found += _period_change(summary, period)
    found += _prompt_composition(summary, cov, period)

    state = "ok"
    if requests == 0:
        state = "no_data"
        found.append(_insight("no_data", "info", "No Copilot requests in this period.", period=period))
    elif requests < MIN_REQUESTS:
        state = "insufficient"
        found.append(_insight(
            "insufficient_evidence", "info",
            "Only %d %s in this period, not enough to say what drives usage."
            % (requests, "request" if requests == 1 else "requests"),
            period=period,
        ))
    found.sort(key=lambda i: SEVERITIES.index(i["severity"]))
    return {
        "period": period,
        "generated_at": datetime.now().astimezone().isoformat(),
        "state": state,
        "coverage": cov,
        "insights": found,
    }
