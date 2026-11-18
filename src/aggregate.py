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
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from src import (
    cli_parser,
    config,
    discovery,
    prompt_composition,
    session_diagnostics,
    vscode_parser,
)
from src.model_labels import ModelLabels
from src.vscode_parser import RequestRecord

_log = logging.getLogger(__name__)

USD_PER_CREDIT = 0.01

# Cache traffic counts at a tenth toward effective tokens: that is the definition
# the README documents, so cards, breakdowns and request detail all agree.
CACHE_WEIGHT = 0.1

VALID_PERIODS = ("today", "week", "month", "cycle", "all")

# A focused date range travels as a period string: "range:2026-11-03..2026-11-09" (inclusive).
RANGE_PREFIX = "range:"

_MAX_DAILY_BUCKETS = 14
_MAX_WEEK_BUCKETS = 12
_MAX_MONTH_BUCKETS = 12
# A focused range of up to this many days is drawn day by day; a longer one week by week.
_MAX_RANGE_DAYS_DAILY = 62
_MAX_RANGE_WEEKS = 52
_TOP_SESSIONS = 8


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


def gather_records() -> Tuple[List[RequestRecord], List[dict]]:
    """Scan every discovered source; return (records, per-source stats)."""
    records: List[RequestRecord] = []
    sources: List[dict] = []

    # (record, source file mtime, source file path, index of its row in ``sources``)
    found: List[Tuple[RequestRecord, float, str, int]] = []
    for root in discovery.discover_roots():
        files = discovery.chat_session_files(root)
        row = len(sources)
        last_mtime = 0.0
        for entry in files:
            path = entry["path"]
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            last_mtime = max(last_mtime, mtime)
            key, name, ppath = vscode_parser.project_from_workspace_dir(entry["workspace_dir"])
            for rec in vscode_parser.parse_session_file(path, root.ide, key, name, ppath):
                found.append((rec, mtime, str(path), row))
        sources.append({
            "ide": root.ide,
            "path": str(root.user_dir),
            "origin": root.origin,
            "kind": "vscode",
            "session_files": len(files),
            "requests": 0,
            "duplicates_ignored": 0,
            "last_activity": _iso_or_none(last_mtime),
        })

    # A chat log copied to a second folder is the same requests twice: count each once.
    losers = vscode_parser.copied_request_losers([(rec, mtime, path) for rec, mtime, path, _ in found])
    for (rec, _mtime, _path, row), loser in zip(found, losers):
        if loser:
            sources[row]["duplicates_ignored"] += 1
        else:
            sources[row]["requests"] += 1
            records.append(rec)
    ignored = sum(s["duplicates_ignored"] for s in sources)
    if ignored:
        _log.info("ℹ️ gather: ignored %d request(s) read from a copied chat log", ignored)

    if config.feature_enabled("copilot_cli"):
        cli_dir = discovery.copilot_cli_dir()
        if cli_dir is not None:
            cli_records, cli_usage = cli_parser.all_records(cli_dir)
            records.extend(cli_records)
            sources.append({
                "ide": "Copilot CLI",
                "path": str(cli_dir),
                "origin": "auto",
                "kind": "cli",
                "session_files": len({r.session_id for r in cli_records}),
                "requests": len(cli_records),
                "last_activity": max((r.ts.isoformat() for r in cli_records), default=None),
                **cli_usage,
            })

    return records, sources


def labels_for(records: List[RequestRecord]) -> ModelLabels:
    """How the models of ``records`` group, with the configured aliases applied.

    Built from every record, never one period's, so a model keeps its label and colour
    whichever period is open.
    """
    aliases = config.load().get("model_aliases") or {}
    return ModelLabels(Counter(r.model for r in records), aliases)


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


def parse_range(period: Any) -> Optional[Tuple[date, date]]:
    """The inclusive (first, last) day of a ``range:`` period, or None when it is not one."""
    if not isinstance(period, str) or not period.startswith(RANGE_PREFIX):
        return None
    first, sep, last = period[len(RANGE_PREFIX):].partition("..")
    try:
        lo, hi = date.fromisoformat(first), date.fromisoformat(last)
    except ValueError:
        return None
    return (lo, hi) if sep and lo <= hi else None


def normalize_period(period: Any, default: str) -> str:
    """``period`` when it names a known period or a well-formed range, else ``default``."""
    if period in VALID_PERIODS or parse_range(period) is not None:
        return period
    return default


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
    focus = parse_range(period)
    if focus is not None:
        return focus
    return None, None


def _prev_bounds(period: str, today: date) -> Optional[Tuple[date, date]]:
    if period == "today":
        return today - timedelta(days=1), today - timedelta(days=1)
    if period == "week":
        return today - timedelta(days=13), today - timedelta(days=7)
    if period == "month":
        return today - timedelta(days=59), today - timedelta(days=30)
    focus = parse_range(period)
    if focus is not None:
        span = (focus[1] - focus[0]).days + 1
        return focus[0] - timedelta(days=span), focus[0] - timedelta(days=1)
    return None


def _in_bounds(d: date, lo: Optional[date], hi: Optional[date]) -> bool:
    return (lo is None or d >= lo) and (hi is None or d <= hi)


# ---------------------------------------------------------------------------
# What the dashboard may see: the telemetry start date and custom session names
# ---------------------------------------------------------------------------


def telemetry_start(cfg: Optional[Dict[str, Any]] = None) -> Optional[date]:
    """The configured first counted day, or None when every record counts."""
    text = (cfg if cfg is not None else config.load()).get("telemetry_start_date") or ""
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def visible(records: List[RequestRecord]) -> List[RequestRecord]:
    """The records every total is built from: nothing before the start date, sessions renamed.

    A renamed session's records are copies whose title is the custom name and whose
    ``parsed_title`` keeps the one the log carried; the parsed records stay untouched.
    """
    cfg = config.load()
    start = telemetry_start(cfg)
    names = cfg.get("session_names") or {}
    if start is not None:
        records = [r for r in records if _local_date(r) >= start]
    if names:
        records = [
            replace(r, session_title=names[r.session_id], parsed_title=r.session_title)
            if r.session_id in names else r
            for r in records
        ]
    return records


def span_coverage(lo: Optional[date], hi: Optional[date], start: Optional[date]) -> str:
    """How much of the span ``lo``..``hi`` (None = open) the start date lets through.

    ``full``: all of it; ``partial``: only its tail, from the start date; ``none``: none of it,
    so a total of 0 there means "not counted", never "no usage".
    """
    if start is None:
        return "full"
    if hi is not None and start > hi:
        return "none"
    if lo is None or start > lo:
        return "partial"
    return "full"


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
        # A token total is None until one request recorded that figure: unknown is not zero.
        "prompt_tokens": None,
        "completion_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "unknown_token_requests": 0,
        "estimated_requests": 0,
    }


def _add_known(total: Optional[int], value: Optional[int]) -> Optional[int]:
    """``total`` plus ``value``, skipping a figure that was not recorded."""
    if value is None:
        return total
    return value if total is None else total + value


def token_metrics(
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    cache_read_tokens: Optional[int],
    cache_write_tokens: Optional[int],
    credits: Optional[float],
) -> Dict[str, Any]:
    """The derived token figures, from one set of definitions.

    ``prompt_tokens`` is the input volume (uncached input plus cache-write);
    cache-read never counts toward it or toward output. A figure that would rest
    on a token count nobody recorded is ``None``, never 0.
    """
    read, write = cache_read_tokens or 0, cache_write_tokens or 0
    uncached = None if prompt_tokens is None else max(prompt_tokens - write, 0)
    eff_in = None if uncached is None else uncached + CACHE_WEIGHT * read
    eff_out = None if completion_tokens is None else completion_tokens + CACHE_WEIGHT * write
    both = eff_in is not None and eff_out is not None
    return {
        "uncached_input_tokens": uncached,
        "total_input_tokens": None if prompt_tokens is None else prompt_tokens + read,
        "effective_input_tokens": None if eff_in is None else round(eff_in),
        "effective_output_tokens": None if eff_out is None else round(eff_out),
        "tokens_per_credit": round((eff_in + eff_out) / credits, 1) if credits and both else None,
    }


_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens")


def _add(acc: Dict[str, Any], r: RequestRecord) -> None:
    acc["requests"] += 1
    for field in _TOKEN_FIELDS:
        acc[field] = _add_known(acc[field], getattr(r, field))
    if r.prompt_tokens is None and r.completion_tokens is None:
        acc["unknown_token_requests"] += 1
    if r.usage_estimated:
        acc["estimated_requests"] += 1
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
    acc.update(token_metrics(
        acc["prompt_tokens"], acc["completion_tokens"],
        acc["cache_read_tokens"], acc["cache_write_tokens"], acc["credits"],
    ))
    return acc


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def get_summary(records: List[RequestRecord], period: str = "today") -> dict:
    period = normalize_period(period, "today")
    today = date.today()
    lo, hi = _period_bounds(period, today)
    start = telemetry_start()
    labels = labels_for(records)

    in_period = [r for r in records if _in_bounds(_local_date(r), lo, hi)]

    totals = _blank()
    for r in in_period:
        _add(totals, r)
    _finish(totals)
    totals["avg_credits_per_request"] = (
        round(totals["credits"] / totals["billed_requests"], 3)
        if totals["billed_requests"] else 0.0
    )

    # A previous span the start date only partly covers would read as a collapse, or as "new".
    prev_totals: Optional[dict] = None
    prev = _prev_bounds(period, today)
    if prev is not None and span_coverage(prev[0], prev[1], start) == "full":
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

    by_model = group_by(lambda r: labels.label(r.model), "model")
    by_group = {g.label: g for g in labels.groups()}
    for row in by_model:
        group = by_group[row["model"]]
        row.update(model_key=group.key, family=group.family, raw_models=list(group.raw))
    by_mode = group_by(lambda r: r.mode or "unknown", "mode")
    by_ide = group_by(lambda r: r.ide, "ide")

    by_project_groups: Dict[str, Dict[str, Any]] = {}
    for r in in_period:
        g = by_project_groups.get(r.project_key)
        if g is None:
            g = by_project_groups[r.project_key] = {
                "project_key": r.project_key,
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

    series, window = _time_series(records, period, today, labels, 0, start)
    return {
        "period": period,
        "generated_at": datetime.now().astimezone().isoformat(),
        "coverage": {
            "state": span_coverage(lo, hi, start),
            "start_date": start.isoformat() if start else None,
        },
        "totals": totals,
        "prev_totals": prev_totals,
        "by_model": by_model,
        "model_meta": labels.meta(),
        "by_mode": by_mode,
        "by_project": by_project,
        "by_ide": by_ide,
        "prompt_mix": prompt_mix(in_period),
        "time_series": series,
        "trend_window": window,
        "top_sessions": build_sessions(in_period, labels)[:_TOP_SESSIONS],
        "budget": budget_status(records, today),
    }


def get_trends(records: List[RequestRecord], period: str = "today", offset: int = 0) -> dict:
    """The trend buckets ``offset`` windows before the latest one, with the window they cover."""
    period = normalize_period(period, "today")
    series, window = _time_series(
        records, period, date.today(), labels_for(records), max(int(offset), 0), telemetry_start(),
    )
    return {"period": period, "time_series": series, "trend_window": window}


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
        entries = prompt_composition.label_tokens(r)
        if not entries:
            continue
        covered_tokens += r.prompt_tokens
        for label, category, tokens in entries:
            tok_by_label[label] += tokens
            cat_by_label.setdefault(label, category)

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


def _month_shift(first_of_month: date, months: int) -> date:
    index = first_of_month.year * 12 + (first_of_month.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def _trend_plan(period: str, today: date, offset: int) -> Tuple[str, List[date]]:
    """The bucket unit and the oldest-first bucket start dates of the window ``offset`` back."""
    focus = parse_range(period)
    if focus is not None:
        lo, hi = focus
        days = (hi - lo).days + 1
        if days <= _MAX_RANGE_DAYS_DAILY:
            return "day", [lo + timedelta(days=i) for i in range(days)]
        last = hi - timedelta(days=hi.weekday())
        weeks = min((last - (lo - timedelta(days=lo.weekday()))).days // 7 + 1, _MAX_RANGE_WEEKS)
        return "week", [last - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    if period in ("today", "cycle"):
        end = today - timedelta(days=_MAX_DAILY_BUCKETS * offset)
        return "day", [end - timedelta(days=i) for i in range(_MAX_DAILY_BUCKETS - 1, -1, -1)]
    if period == "week":
        end = today - timedelta(days=today.weekday()) - timedelta(weeks=_MAX_WEEK_BUCKETS * offset)
        return "week", [end - timedelta(weeks=i) for i in range(_MAX_WEEK_BUCKETS - 1, -1, -1)]
    newest = _month_shift(today.replace(day=1), -_MAX_MONTH_BUCKETS * offset)
    return "month", [_month_shift(newest, -i) for i in range(_MAX_MONTH_BUCKETS - 1, -1, -1)]


def _bucket_end(unit: str, start: date) -> date:
    if unit == "day":
        return start
    if unit == "week":
        return start + timedelta(days=6)
    return _month_shift(start, 1) - timedelta(days=1)


def _bucket_key(unit: str, d: date) -> date:
    if unit == "day":
        return d
    if unit == "week":
        return d - timedelta(days=d.weekday())
    return date(d.year, d.month, 1)


def _time_series(
    records: List[RequestRecord], period: str, today: date, labels: ModelLabels,
    offset: int, start: Optional[date],
) -> Tuple[List[dict], dict]:
    """Oldest-first buckets with per-model credits/tokens/requests, and the window they cover.

    ``offset`` steps back one whole window per unit; a focused range is its own window. Each
    bucket says how much of its span the start date covers, and ends no later than today (or
    the end of the focused range), so a tap on it names the days to focus on.
    """
    unit, buckets = _trend_plan(period, today, offset)
    focus = parse_range(period)
    clip_hi = min(focus[1], today) if focus else today
    clip_lo = focus[0] if focus else None
    spans: Dict[date, Tuple[date, date]] = {}
    for b in buckets:
        first = max(b, clip_lo) if clip_lo else b
        spans[b] = (first, min(_bucket_end(unit, b), clip_hi))

    bmap: Dict[date, Dict[str, dict]] = {b: {} for b in buckets}
    earliest: Optional[date] = None
    for r in records:
        d = _local_date(r)
        if earliest is None or d < earliest:
            earliest = d
        bk = _bucket_key(unit, d)
        if bk not in bmap or (focus and not _in_bounds(d, focus[0], focus[1])):
            continue
        label = labels.label(r.model)
        slot = bmap[bk].get(label)
        if slot is None:
            slot = bmap[bk][label] = {
                "credits": 0.0, "requests": 0,
                "prompt_tokens": None, "completion_tokens": None,
                "cache_read_tokens": None, "cache_write_tokens": None,
            }
        slot["requests"] += 1
        slot["credits"] = round(slot["credits"] + (r.credits or 0.0), 4)
        for field in _TOKEN_FIELDS:
            slot[field] = _add_known(slot[field], getattr(r, field))

    series = [
        {
            "label": b.strftime("%b %Y") if unit == "month" else b.strftime("%b ") + str(b.day),
            "date": spans[b][0].isoformat(),
            "end": spans[b][1].isoformat(),
            "coverage": span_coverage(spans[b][0], spans[b][1], start),
            "models": bmap[b],
        }
        for b in buckets
    ]
    window = {
        "unit": unit,
        "offset": offset,
        "from": series[0]["date"],
        "to": series[-1]["end"],
        "has_older": focus is None and earliest is not None and earliest < buckets[0],
        "has_newer": offset > 0,
    }
    return series, window


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
        "coverage": span_coverage(start, today, telemetry_start(cfg)),
        "note": "local data from this machine only",
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def build_sessions(records: List[RequestRecord], labels: Optional[ModelLabels] = None) -> List[dict]:
    """Group records into per-session rows, most expensive first.

    ``models`` lists the grouped labels; pass the labels of the full history so a
    subset of the records does not relabel a model.
    """
    labels = labels or labels_for(records)
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
                "parsed_title": r.parsed_title or r.session_title,
                "custom_name": r.session_title if r.parsed_title else None,
                "project": r.project_name,
                "project_path": r.project_path,
                "first_ts": r.ts.isoformat(),
                "last_ts": r.ts.isoformat(),
                "models": [],
                "modes": [],
                "usage_sources": [],
                **_blank(),
            }
        _add(s, r)
        iso = r.ts.isoformat()
        if iso < s["first_ts"]:
            s["first_ts"] = iso
        if iso > s["last_ts"]:
            s["last_ts"] = iso
        model = labels.label(r.model)
        if model not in s["models"]:
            s["models"].append(model)
        if r.mode and r.mode not in s["modes"]:
            s["modes"].append(r.mode)
        if r.usage_source and r.usage_source not in s["usage_sources"]:
            s["usage_sources"].append(r.usage_source)

    rows = [_finish(s) for s in smap.values()]
    rows.sort(key=lambda x: (x["credits"], x["last_ts"]), reverse=True)
    return rows


def records_in_period(records: List[RequestRecord], period: str) -> List[RequestRecord]:
    """The records whose local date falls in ``period``; an unknown period means all time."""
    period = normalize_period(period, "all")
    lo, hi = _period_bounds(period, date.today())
    return [r for r in records if _in_bounds(_local_date(r), lo, hi)]


def get_sessions(records: List[RequestRecord], period: str = "all") -> List[dict]:
    in_period = records_in_period(records, period)
    return session_diagnostics.attach_flags(
        build_sessions(in_period, labels_for(records)), in_period, records,
    )


def get_session_detail(records: List[RequestRecord], session_id: str) -> Optional[dict]:
    """Per-request drill-down for one session (all-time, ignores period)."""
    sess = [r for r in records if r.session_id == session_id]
    if not sess:
        return None
    sess.sort(key=lambda r: r.ts)
    labels = labels_for(records)
    header = build_sessions(sess, labels)[0]
    header["requests_detail"] = [
        {
            "request_id": r.request_id,
            "ts": r.ts.isoformat(),
            "message": r.message,
            "mode": r.mode,
            "model": r.model,
            "model_label": labels.label(r.model),
            "model_requested": r.model_requested,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "cache_write_tokens": r.cache_write_tokens,
            **token_metrics(
                r.prompt_tokens, r.completion_tokens,
                r.cache_read_tokens, r.cache_write_tokens, r.credits,
            ),
            "usage_source": r.usage_source,
            "usage_estimated": r.usage_estimated,
            "credits": r.credits,
            "usd": round(r.credits * USD_PER_CREDIT, 4) if r.credits is not None else None,
            "elapsed_ms": r.elapsed_ms,
            "prompt_details": r.prompt_details,
            "context_items": r.context_items,
            "error": r.error,
        }
        for r in sess
    ]
    header["prompt_mix"] = prompt_mix(sess)
    header["diagnostics"] = session_diagnostics.diagnose(sess, session_diagnostics.non_user_cut(records))
    return header


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

_CSV_COLUMNS = [
    "timestamp", "ide", "source", "project", "session_id", "session_title",
    "request_id", "mode", "model", "model_requested", "prompt_tokens",
    "completion_tokens", "cache_read_tokens", "cache_write_tokens", "credits",
    "usd", "elapsed_ms", "error", "usage_source", "usage_estimated", "message",
]


def export_csv(records: List[RequestRecord], period: str = "all") -> str:
    period = normalize_period(period, "all")
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
            r.cache_read_tokens, r.cache_write_tokens,
            "" if r.credits is None else r.credits,
            "" if r.credits is None else round(r.credits * USD_PER_CREDIT, 4),
            "" if r.elapsed_ms is None else r.elapsed_ms,
            int(r.error), r.usage_source, int(r.usage_estimated), r.message,
        ])
    return buf.getvalue()
