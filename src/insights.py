"""Deterministic, local insights over a period's usage.

Four detectors (top contributors, period-over-period change, budget risk, prompt
composition) read the same figures the dashboard cards show. No model call, no network.
Each insight has a stable ``type``, a ``severity`` (``info`` | ``attention`` | ``warning`` |
``critical``), one short sentence and structured ``evidence`` (period, model, project,
session, label) the UI turns into a link to the matching view. A detector that lacks the
data says nothing rather than guess; the result's ``state`` is always one of ``no_data``,
``insufficient_data``, ``no_actionable_findings`` or ``ready``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from src import aggregate, context_drilldown, prompt_composition
from src.vscode_parser import RequestRecord

TYPES = (
    "top_contributor", "period_change", "budget_risk", "prompt_composition",
    "no_data", "insufficient_data",
)
SEVERITIES = ("critical", "warning", "attention", "info")
STATES = ("no_data", "insufficient_data", "no_actionable_findings", "ready")

# The largest model, project and session are named whenever the period billed credits; one
# at this share of the spend or more is "attention", otherwise plain "info".
TOP_SHARE_PCT = 50.0
# A change is worth a line when it is at least this many credits and this share of the
# previous period's spend, so a small period's noise and a large period's drift both stay quiet.
MIN_CHANGE_CREDITS = 0.5
MIN_CHANGE_RATIO = 0.20
# A prompt mix is only stated when enough requests carry one, it covers enough of the
# period's prompt tokens, and most of what it observed is outside the user's own context.
COMPOSITION_MIN_COVERED = 2
COMPOSITION_MIN_TOKENS_PCT = 50.0
COMPOSITION_MIN_NON_USER_PCT = 70.0

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
    credits = summary["totals"]["credits"]
    if credits <= 0:
        return []
    out: List[Dict[str, Any]] = []
    shared = "%(name)s used %(share)s%% of this period's credits (%(spent)s of %(all)s)."
    # dimension -> (rows, sentence, name field, evidence key field)
    top = {
        "model": (summary["by_model"], shared, "model", "model"),
        "project": (summary["by_project"], shared, "project", "project_key"),
        "session": (summary["top_sessions"], "One session, \"%(name)s\", used %(share)s%% of this period's credits.",
                    "title", "session_id"),
    }
    for dimension, (rows, template, name_field, key_field) in top.items():
        if not rows or rows[0]["credits"] <= 0:
            continue
        row = rows[0]
        raw_share = 100.0 * row["credits"] / credits
        text = template % {
            "name": row[name_field], "share": round(raw_share, 1),
            "spent": _credits(row["credits"]), "all": _credits(credits),
        }
        key = row[key_field]
        out.append(_insight(
            "top_contributor", "attention" if raw_share >= TOP_SHARE_PCT else "info", text,
            period=period, **{dimension: key},
        ))
    return out


def _period_change(summary: Dict[str, Any], period: str) -> List[Dict[str, Any]]:
    prev = summary["prev_totals"]
    if prev is None:
        return []
    now = summary["totals"]["credits"]
    before = prev["credits"]
    delta = round(now - before, 6)
    if abs(delta) < max(MIN_CHANGE_CREDITS, round(MIN_CHANGE_RATIO * before, 6)):
        return []
    span = _PREVIOUS.get(period, "the span before")
    if before > 0:
        text = "Credits are %s %d%% on %s (%s vs %s)." % (
            "up" if delta > 0 else "down", round(100.0 * abs(delta) / before), span, _credits(now), _credits(before),
        )
    else:
        text = "Credits are up on %s: %s, against none then." % (span, _credits(now))
    return [_insight("period_change", "warning" if delta > 0 else "info", text, period=period)]


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
    if cov["requests_covered"] < COMPOSITION_MIN_COVERED or (cov["tokens_pct"] or 0) < COMPOSITION_MIN_TOKENS_PCT:
        return []
    mix = summary["prompt_mix"]
    overhead = [row for row in mix if row["category"] != prompt_composition.USER_CATEGORY]
    non_user_pct = round(sum(row["pct"] for row in overhead), 1)
    if not overhead or non_user_pct < COMPOSITION_MIN_NON_USER_PCT:
        return []
    top = overhead[0]
    is_tools = "tool" in top["label"].lower()
    text = (
        "%s is about %s%% of prompt tokens; %s%% is outside your own context (%s%% of this period's tokens "
        "carry a breakdown)." % (top["label"], top["pct"], non_user_pct, round(cov["tokens_pct"]))
    )
    if is_tools:
        text += " Disabling unused tools and MCP servers shrinks every request."
    return [_insight(
        "prompt_composition", "warning" if is_tools else "info", text, period=period, label=top["label"],
    )]


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

    if requests == 0:
        state = "no_data"
        found.append(_insight("no_data", "info", "No Copilot requests in this period.", period=period))
    elif summary["totals"]["credits"] <= 0:
        state = "insufficient_data"
        found.append(_insight(
            "insufficient_data", "info",
            "%d %s in this period billed no credits, so there is no spend to attribute."
            % (requests, "request" if requests == 1 else "requests"),
            period=period,
        ))
    else:
        state = "ready" if found else "no_actionable_findings"
    found.sort(key=lambda i: SEVERITIES.index(i["severity"]))
    return {
        "period": period,
        "generated_at": datetime.now().astimezone().isoformat(),
        "state": state,
        "coverage": cov,
        "insights": found,
    }
