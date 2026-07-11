"""Aggregation: raw RequestRecords -> dashboard-shaped summaries.

All period bucketing uses the machine's **local** date (``today`` means today
at your desk, not UTC) — usage stats are a human-facing report, not a billing
reconciliation. The optional GitHub billing card (``billing.py``) is the
UTC-exact counterpart.

Credits: 1 Copilot credit == 1 premium-request unit == $0.01 on GitHub's
AI-credits billing model. ``credits`` is the primary unit everywhere; USD is
derived for display.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app import cli_parser, config, discovery, vscode_parser
from app.vscode_parser import RequestRecord

_log = logging.getLogger(__name__)

USD_PER_CREDIT = 0.01

VALID_PERIODS = ("today", "week", "month", "cycle", "all")

_MAX_DAILY_BUCKETS = 14
_MAX_WEEK_BUCKETS = 12
_MAX_MONTH_BUCKETS = 12
_TOP_SESSIONS = 8


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


def gather_records() -> Tuple[List[RequestRecord], List[dict]]:
    """Scan every discovered source; return (records, per-source stats)."""
    records: List[RequestRecord] = []
    sources: List[dict] = []

    for root in discovery.discover_roots():
        files = discovery.chat_session_files(root)
        root_records: List[RequestRecord] = []
        last_mtime = 0.0
        for entry in files:
            path = entry["path"]
            try:
                last_mtime = max(last_mtime, path.stat().st_mtime)
            except OSError:
                pass
            key, name, ppath = vscode_parser.project_from_workspace_dir(entry["workspace_dir"])
            root_records.extend(
                vscode_parser.parse_session_file(path, root.ide, key, name, ppath)
            )
        records.extend(root_records)
        sources.append({
            "ide": root.ide,
            "path": str(root.user_dir),
            "origin": root.origin,
            "kind": "vscode",
            "session_files": len(files),
            "requests": len(root_records),
            "last_activity": _iso_or_none(last_mtime),
        })

    if config.load().get("include_copilot_cli", True):
        cli_dir = discovery.copilot_cli_dir()
        if cli_dir is not None:
            cli_records = cli_parser.all_records(cli_dir)
            records.extend(cli_records)
            sources.append({
                "ide": "Copilot CLI",
                "path": str(cli_dir),
                "origin": "auto",
                "kind": "cli",
                "session_files": len({r.session_id for r in cli_records}),
                "requests": len(cli_records),
                "last_activity": max((r.ts.isoformat() for r in cli_records), default=None),
            })

    return records, sources


def _iso_or_none(mtime: float) -> Optional[str]:
    if not mtime:
        return None
    return datetime.fromtimestamp(mtime).astimezone().isoformat()


# ---------------------------------------------------------------------------
# Period helpers (local dates)
# ---------------------------------------------------------------------------


def _local_date(r: RequestRecord) -> date:
    return r.ts.astimezone().date()


def cycle_start(today: Optional[date] = None) -> date:
    """First day of the current billing cycle per config.cycle_reset_day."""
    today = today or date.today()
    reset_day = int(config.load().get("cycle_reset_day", 1))
    if today.day >= reset_day:
        return date(today.year, today.month, min(reset_day, monthrange(today.year, today.month)[1]))
    prev_y, prev_m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return date(prev_y, prev_m, min(reset_day, monthrange(prev_y, prev_m)[1]))


def _period_bounds(period: str, today: date) -> Tuple[Optional[date], Optional[date]]:
    """Inclusive (lo, hi) local-date bounds; (None, None) = all time."""
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=6), today
    if period == "month":
        return today - timedelta(days=29), today
    if period == "cycle":
        return cycle_start(today), today
    return None, None


def _prev_bounds(period: str, today: date) -> Optional[Tuple[date, date]]:
    if period == "today":
        return today - timedelta(days=1), today - timedelta(days=1)
    if period == "week":
        return today - timedelta(days=13), today - timedelta(days=7)
    if period == "month":
        return today - timedelta(days=59), today - timedelta(days=30)
    return None


def _in_bounds(d: date, lo: Optional[date], hi: Optional[date]) -> bool:
    return (lo is None or d >= lo) and (hi is None or d <= hi)


# ---------------------------------------------------------------------------
# Accumulators
# ---------------------------------------------------------------------------


def _blank() -> Dict[str, Any]:
    return {
        "requests": 0,
        "billed_requests": 0,
        "errors": 0,
        "credits": 0.0,
        "error_credits": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


def _add(acc: Dict[str, Any], r: RequestRecord) -> None:
    acc["requests"] += 1
    acc["prompt_tokens"] += r.prompt_tokens
    acc["completion_tokens"] += r.completion_tokens
    if r.credits is not None:
        acc["billed_requests"] += 1
        acc["credits"] += r.credits
    if r.error:
        acc["errors"] += 1
        if r.credits is not None:
            acc["error_credits"] += r.credits


def _finish(acc: Dict[str, Any]) -> Dict[str, Any]:
    acc["credits"] = round(acc["credits"], 4)
    acc["error_credits"] = round(acc["error_credits"], 4)
    acc["usd"] = round(acc["credits"] * USD_PER_CREDIT, 4)
    return acc


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def get_summary(records: List[RequestRecord], period: str = "today") -> dict:
    if period not in VALID_PERIODS:
        period = "today"
    today = date.today()
    lo, hi = _period_bounds(period, today)

    in_period = [r for r in records if _in_bounds(_local_date(r), lo, hi)]

    totals = _blank()
    for r in in_period:
        _add(totals, r)
    _finish(totals)
    totals["avg_credits_per_request"] = (
        round(totals["credits"] / totals["billed_requests"], 3)
        if totals["billed_requests"] else 0.0
    )

    prev_totals: Optional[dict] = None
    prev = _prev_bounds(period, today)
    if prev is not None:
        prev_totals = _blank()
        for r in records:
            if _in_bounds(_local_date(r), prev[0], prev[1]):
                _add(prev_totals, r)
        _finish(prev_totals)

    def group_by(key_fn, label_field: str) -> List[dict]:
        groups: Dict[str, Dict[str, Any]] = {}
        for r in in_period:
            k = key_fn(r) or "unknown"
            g = groups.get(k)
            if g is None:
                g = groups[k] = {label_field: k, **_blank()}
            _add(g, r)
        rows = [_finish(g) for g in groups.values()]
        rows.sort(key=lambda x: (x["credits"], x["requests"]), reverse=True)
        total_credits = sum(x["credits"] for x in rows) or 1.0
        for x in rows:
            x["credits_share_pct"] = round(100.0 * x["credits"] / total_credits, 1)
        return rows

    by_model = group_by(lambda r: r.model, "model")
    by_mode = group_by(lambda r: r.mode or "unknown", "mode")
    by_ide = group_by(lambda r: r.ide, "ide")

    by_project_groups: Dict[str, Dict[str, Any]] = {}
    for r in in_period:
        g = by_project_groups.get(r.project_key)
        if g is None:
            g = by_project_groups[r.project_key] = {
                "project": r.project_name,
                "project_path": r.project_path,
                **_blank(),
            }
        _add(g, r)
    by_project = sorted(
        (_finish(g) for g in by_project_groups.values()),
        key=lambda x: (x["credits"], x["requests"]),
        reverse=True,
    )

    return {
        "period": period,
        "generated_at": datetime.now().astimezone().isoformat(),
        "totals": totals,
        "prev_totals": prev_totals,
        "by_model": by_model,
        "by_mode": by_mode,
        "by_project": by_project,
        "by_ide": by_ide,
        "prompt_mix": prompt_mix(in_period),
        "time_series": _time_series(records, period, today),
        "top_sessions": build_sessions(in_period)[:_TOP_SESSIONS],
        "budget": budget_status(records, today),
    }


def prompt_mix(records: List[RequestRecord]) -> List[dict]:
    """Weighted prompt-composition breakdown across requests that carry
    ``promptTokenDetails`` — answers "where do my input tokens actually go".

    Estimated tokens per label = sum(prompt_tokens * pct/100); the weighted
    percentage uses prompt_tokens as the weight.
    """
    tok_by_label: Dict[str, float] = defaultdict(float)
    cat_by_label: Dict[str, str] = {}
    covered_tokens = 0
    for r in records:
        if not r.prompt_details or r.prompt_tokens <= 0:
            continue
        covered_tokens += r.prompt_tokens
        for d in r.prompt_details:
            if not isinstance(d, dict):
                continue
            label = str(d.get("label") or d.get("category") or "Other")
            pct = d.get("percentageOfPrompt")
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                continue
            tok_by_label[label] += r.prompt_tokens * pct / 100.0
            cat_by_label.setdefault(label, str(d.get("category") or ""))

    if not covered_tokens:
        return []
    rows = [
        {
            "label": label,
            "category": cat_by_label.get(label, ""),
            "est_tokens": int(tok),
            "pct": round(100.0 * tok / covered_tokens, 1),
        }
        for label, tok in tok_by_label.items()
    ]
    rows.sort(key=lambda x: x["est_tokens"], reverse=True)
    return rows


def _time_series(records: List[RequestRecord], period: str, today: date) -> List[dict]:
    """Oldest-first buckets with per-model credits/tokens/requests."""
    if period in ("today", "cycle"):
        buckets = [today - timedelta(days=i) for i in range(_MAX_DAILY_BUCKETS - 1, -1, -1)]
        key_fn = lambda d: d  # noqa: E731
        label_fn = lambda b: b.strftime("%b ") + str(b.day)  # noqa: E731
    elif period == "week":
        this_mon = today - timedelta(days=today.weekday())
        buckets = [this_mon - timedelta(weeks=i) for i in range(_MAX_WEEK_BUCKETS - 1, -1, -1)]
        key_fn = lambda d: d - timedelta(days=d.weekday())  # noqa: E731
        label_fn = lambda b: b.strftime("%b ") + str(b.day)  # noqa: E731
    else:  # month, all
        ym: List[Tuple[int, int]] = []
        y, m = today.year, today.month
        for _ in range(_MAX_MONTH_BUCKETS):
            ym.append((y, m))
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        buckets = [date(yy, mm, 1) for yy, mm in reversed(ym)]
        key_fn = lambda d: date(d.year, d.month, 1)  # noqa: E731
        label_fn = lambda b: b.strftime("%b %Y")  # noqa: E731

    bucket_set = set(buckets)
    bmap: Dict[date, Dict[str, dict]] = {b: {} for b in buckets}
    for r in records:
        bk = key_fn(_local_date(r))
        if bk not in bucket_set:
            continue
        slot = bmap[bk].get(r.model)
        if slot is None:
            slot = bmap[bk][r.model] = {
                "credits": 0.0, "requests": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
            }
        slot["requests"] += 1
        slot["credits"] = round(slot["credits"] + (r.credits or 0.0), 4)
        slot["prompt_tokens"] += r.prompt_tokens
        slot["completion_tokens"] += r.completion_tokens

    return [{"label": label_fn(b), "date": b.isoformat(), "models": bmap[b]} for b in buckets]


# ---------------------------------------------------------------------------
# Budget (billing-cycle burn-down)
# ---------------------------------------------------------------------------


def budget_status(records: List[RequestRecord], today: Optional[date] = None) -> dict:
    today = today or date.today()
    cfg = config.load()
    allowance = int(cfg.get("monthly_credits", 300))
    start = cycle_start(today)

    if start.month == 12:
        next_anchor = date(start.year + 1, 1, 1)
    else:
        next_anchor = date(start.year, start.month + 1, 1)
    reset_day = int(cfg.get("cycle_reset_day", 1))
    end = date(
        next_anchor.year, next_anchor.month,
        min(reset_day, monthrange(next_anchor.year, next_anchor.month)[1]),
    ) - timedelta(days=1)

    used = sum(
        (r.credits or 0.0)
        for r in records
        if _in_bounds(_local_date(r), start, today)
    )
    days_total = (end - start).days + 1
    days_elapsed = (today - start).days + 1
    projected = used / days_elapsed * days_total if days_elapsed else used

    return {
        "cycle_start": start.isoformat(),
        "cycle_end": end.isoformat(),
        "days_elapsed": days_elapsed,
        "days_total": days_total,
        "allowance_credits": allowance,
        "used_credits": round(used, 2),
        "used_pct": round(100.0 * used / allowance, 1) if allowance else None,
        "projected_credits": round(projected, 1),
        "projected_pct": round(100.0 * projected / allowance, 1) if allowance else None,
        "note": "local data from this machine only",
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def build_sessions(records: List[RequestRecord]) -> List[dict]:
    """Group records into per-session rows, most expensive first."""
    smap: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in records:
        k = (r.ide, r.session_id)
        s = smap.get(k)
        if s is None:
            s = smap[k] = {
                "session_id": r.session_id,
                "ide": r.ide,
                "source": r.source,
                "title": r.session_title,
                "project": r.project_name,
                "project_path": r.project_path,
                "first_ts": r.ts.isoformat(),
                "last_ts": r.ts.isoformat(),
                "models": [],
                "modes": [],
                **_blank(),
            }
        _add(s, r)
        iso = r.ts.isoformat()
        if iso < s["first_ts"]:
            s["first_ts"] = iso
        if iso > s["last_ts"]:
            s["last_ts"] = iso
        if r.model not in s["models"]:
            s["models"].append(r.model)
        if r.mode and r.mode not in s["modes"]:
            s["modes"].append(r.mode)

    rows = [_finish(s) for s in smap.values()]
    rows.sort(key=lambda x: (x["credits"], x["last_ts"]), reverse=True)
    return rows


def get_sessions(records: List[RequestRecord], period: str = "all") -> List[dict]:
    if period not in VALID_PERIODS:
        period = "all"
    lo, hi = _period_bounds(period, date.today())
    return build_sessions([r for r in records if _in_bounds(_local_date(r), lo, hi)])


def get_session_detail(records: List[RequestRecord], session_id: str) -> Optional[dict]:
    """Per-request drill-down for one session (all-time, ignores period)."""
    sess = [r for r in records if r.session_id == session_id]
    if not sess:
        return None
    sess.sort(key=lambda r: r.ts)
    header = build_sessions(sess)[0]
    header["requests_detail"] = [
        {
            "request_id": r.request_id,
            "ts": r.ts.isoformat(),
            "message": r.message,
            "mode": r.mode,
            "model": r.model,
            "model_requested": r.model_requested,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "credits": r.credits,
            "usd": round(r.credits * USD_PER_CREDIT, 4) if r.credits is not None else None,
            "elapsed_ms": r.elapsed_ms,
            "prompt_details": r.prompt_details,
            "error": r.error,
        }
        for r in sess
    ]
    header["prompt_mix"] = prompt_mix(sess)
    return header


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

_CSV_COLUMNS = [
    "timestamp", "ide", "source", "project", "session_id", "session_title",
    "request_id", "mode", "model", "model_requested", "prompt_tokens",
    "completion_tokens", "credits", "usd", "elapsed_ms", "error", "message",
]


def export_csv(records: List[RequestRecord], period: str = "all") -> str:
    if period not in VALID_PERIODS:
        period = "all"
    lo, hi = _period_bounds(period, date.today())
    rows = sorted(
        (r for r in records if _in_bounds(_local_date(r), lo, hi)),
        key=lambda r: r.ts,
    )
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    for r in rows:
        writer.writerow([
            r.ts.astimezone().isoformat(), r.ide, r.source, r.project_name,
            r.session_id, r.session_title, r.request_id, r.mode, r.model,
            r.model_requested, r.prompt_tokens, r.completion_tokens,
            "" if r.credits is None else r.credits,
            "" if r.credits is None else round(r.credits * USD_PER_CREDIT, 4),
            "" if r.elapsed_ms is None else r.elapsed_ms,
            int(r.error), r.message,
        ])
    return buf.getvalue()
