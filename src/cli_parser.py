"""Parser for GitHub Copilot CLI usage (optional source).

Two sources, best first:

1. ``~/.copilot/session-store.db`` (``cli_session_store``): one usage row per
   model call, so each prompt becomes its own record with its own time and
   model — active sessions and sub-agent calls included. ``usage_source`` is
   ``session-store``.
2. ``~/.copilot/session-state/<uuid>/events.jsonl`` for every session the
   database does not cover (database missing, unknown shape, or an older
   session). ``session.shutdown`` carries exact per-model totals; its figures
   are cumulative across resumes, so only the last one counts. When there is no
   shutdown yet, the last cumulative ``session.usage_checkpoint`` gives the
   credits so far. Those totals are spread over the session's
   ``user.message`` prompts and marked ``usage_estimated``: each checkpoint's
   delta (and the shutdown remainder past the last one) is split over the prompts
   in its interval by their assistant output tokens, evenly without that hint. A single
   prompt, or no prompt at all, keeps the exact totals unmarked.

``prompt_tokens`` on these records is the comparable input volume (uncached
input plus cache-write); cache-read stays in ``cache_read_tokens``. Credits =
nano AI-usage units / 1e9.

Prompt text is kept whole (the CLI logs it whole; lists truncate with CSS).
A timestamp with no zone is host-local time; one with ``Z`` or an offset keeps
its instant. A prompt replayed on resume carries the same ``interactionId`` and
counts once.

``workspace.yaml`` is trivially flat, so the single ``cwd:`` line is read with
a string scan instead of pulling in a YAML dependency.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src import cli_session_store
from src.cli_session_store import StoreSnapshot
from src.vscode_parser import RequestRecord, _MtimeCache, optional_int

_log = logging.getLogger(__name__)

_CHECKPOINT_SOURCE = "events.jsonl-checkpoint"
# The source of a record whose input and output tokens the CLI never recorded.
UNAVAILABLE_SOURCE = "unavailable"

_cache = _MtimeCache()
# The last snapshot and the records built from it; rebuilt when the snapshot object changes.
_store_records_cache: List[tuple] = []


def clear_caches() -> None:
    """Drop the parsed events files, the records built from the store and the store's own read."""
    _cache.clear()
    _store_records_cache.clear()
    cli_session_store.clear_cache()


@dataclass
class _Usage:
    """Token and credit totals for one model; ``input_tokens`` includes cache traffic.

    A token figure the source did not record is ``None``, which stays apart from 0.
    ``uncached_input_tokens`` is the uncached input the source states outright, when it does.
    """

    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    nano_aiu: float = 0.0
    uncached_input_tokens: Optional[int] = None


def _sum_known(values: List[Optional[int]]) -> Optional[int]:
    """The sum of the figures that were recorded; ``None`` when none were."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _read_cwd(session_dir: Path) -> Optional[str]:
    try:
        text = (session_dir / "workspace.yaml").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("cwd:"):
            value = stripped[len("cwd:"):].strip().strip('"').strip("'")
            return value or None
    return None


def _host_tz() -> Optional[tzinfo]:
    """The zone a timestamp without one is read in; ``None`` is the host's own local zone."""
    return None


def _parse_ts(raw: Optional[str]) -> datetime:
    """An instant in UTC: an aware value keeps its instant, a naive one is host-local time."""
    try:
        text = str(raw).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_host_tz())
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return datetime.now(tz=timezone.utc)


def _project(cwd: Optional[str]) -> Tuple[str, str, Optional[str]]:
    name = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if cwd else "(unknown)"
    return (cwd or "copilot-cli").lower(), name, cwd


def _record(
    session_id: str,
    cwd: Optional[str],
    request_id: str,
    ts: datetime,
    message: str,
    usage: _Usage,
    elapsed_ms: Optional[int],
    usage_source: str,
    estimated: bool,
) -> RequestRecord:
    key, name, path = _project(cwd)
    cache_read, cache_write = usage.cache_read_tokens, usage.cache_write_tokens
    uncached = usage.uncached_input_tokens
    if uncached is None and usage.input_tokens is not None:
        uncached = max(usage.input_tokens - (cache_read or 0) - (cache_write or 0), 0)
    if uncached is not None:
        # An input figure was recorded, so a cache bucket it left out held nothing.
        cache_read, cache_write = cache_read or 0, cache_write or 0
    prompt = None if uncached is None else uncached + (cache_write or 0)
    if prompt is None and usage.output_tokens is None and usage_source != _CHECKPOINT_SOURCE:
        usage_source = UNAVAILABLE_SOURCE
    return RequestRecord(
        ide="Copilot CLI",
        source="cli",
        project_key=key,
        project_name=name,
        project_path=path,
        session_id=session_id,
        session_title="CLI session — " + name,
        request_id=request_id,
        ts=ts,
        message=message,
        mode="cli",
        model_requested=usage.model,
        model=usage.model,
        prompt_tokens=prompt,
        completion_tokens=usage.output_tokens,
        credits=usage.nano_aiu / 1e9,
        elapsed_ms=elapsed_ms,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        usage_source=usage_source,
        usage_estimated=estimated,
    )


# ---------------------------------------------------------------------------
# session-store.db: exact per-prompt records
# ---------------------------------------------------------------------------


def _call_uncached(call: cli_session_store.UsageCall) -> Optional[int]:
    """One call uncached input: the store exact figure, else total input less the cache buckets."""
    if call.uncached_input_tokens is not None:
        return call.uncached_input_tokens
    if call.input_tokens is None:
        return None
    return max(call.input_tokens - (call.cache_read_tokens or 0) - (call.cache_write_tokens or 0), 0)


def _store_records(store: StoreSnapshot, session_state_dir: Path) -> List[RequestRecord]:
    """One record per (session, turn, model); sub-agent calls fold into their turn."""
    groups: Dict[tuple, List[cli_session_store.UsageCall]] = {}
    for call in store.calls:
        turn_key = call.turn_index if call.turn_index is not None else "t" + call.created_at
        groups.setdefault((call.session_id, turn_key, call.model), []).append(call)

    cwds: Dict[str, Optional[str]] = {}
    records: List[RequestRecord] = []
    for (sid, turn_key, model), calls in groups.items():
        if sid not in cwds:
            cwds[sid] = store.cwds.get(sid) or _read_cwd(session_state_dir / sid)
        turn = store.turns.get((sid, turn_key)) if isinstance(turn_key, int) else None
        first_call = min(c.created_at for c in calls)
        durations = [c.duration_ms for c in calls if c.duration_ms is not None]
        usage = _Usage(
            model=model,
            input_tokens=_sum_known([c.input_tokens for c in calls]),
            output_tokens=_sum_known([c.output_tokens for c in calls]),
            cache_read_tokens=_sum_known([c.cache_read_tokens for c in calls]),
            cache_write_tokens=_sum_known([c.cache_write_tokens for c in calls]),
            nano_aiu=float(sum(c.nano_aiu for c in calls)),
            uncached_input_tokens=_sum_known([_call_uncached(c) for c in calls]),
        )
        records.append(_record(
            sid, cwds[sid], sid + ":" + str(turn_key) + ":" + model,
            _parse_ts(turn[0] if turn and turn[0] else first_call),
            turn[1] if turn else "",
            usage, sum(durations) if durations else None, "session-store", False,
        ))
    return records


# ---------------------------------------------------------------------------
# events.jsonl: session totals, allocated across prompts
# ---------------------------------------------------------------------------


def _split_int(total: Optional[int], weights: List[float]) -> List[Optional[int]]:
    """Split ``total`` by ``weights`` into integers that still sum to ``total``; an unrecorded total stays unrecorded."""
    if total is None:
        return [None] * len(weights)
    wsum = sum(weights)
    raw = [total * w / wsum for w in weights]
    parts = [int(x) for x in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - parts[i], reverse=True)
    for i in order[: total - sum(parts)]:
        parts[i] += 1
    return parts


def _spread(prompts: List[datetime], hints: List[int], start: Optional[datetime], end: Optional[datetime]) -> List[Tuple[int, float]]:
    """Shares (prompt index, fraction) for the credits of one interval ``(start, end]``.

    Prompts inside the interval split it by their assistant output tokens, evenly when none
    carries a hint. An interval with no prompt falls back to the prompts before ``end``, then to
    the last prompt, so a delta is never dropped.
    """
    members = [i for i, p in enumerate(prompts) if (start is None or p > start) and (end is None or p <= end)]
    if not members:
        members = [i for i, p in enumerate(prompts) if end is None or p <= end]
    if not members:
        members = [len(prompts) - 1]
    weights = [float(hints[i]) for i in members]
    if sum(weights) <= 0:
        weights = [1.0] * len(members)
    total = sum(weights)
    return [(i, w / total) for i, w in zip(members, weights)]


def _prompt_weights(
    prompts: List[datetime],
    hints: List[int],
    checkpoints: List[Tuple[datetime, float]],
    session_total: Optional[float] = None,
) -> List[float]:
    """Credit weight per prompt: each checkpoint delta is spread over the prompts in its interval.

    A checkpoint below the highest total so far is ignored. ``session_total`` (the shutdown's
    credits) adds the remainder past the last checkpoint over the prompts after it.
    """
    weights = [0.0] * len(prompts)
    start: Optional[datetime] = None
    prev = 0.0
    for ts, total in checkpoints:
        if total < prev:
            continue
        if total > prev:
            for idx, share in _spread(prompts, hints, start, ts):
                weights[idx] += (total - prev) * share
        start, prev = ts, total
    if session_total is not None and session_total > prev:
        for idx, share in _spread(prompts, hints, start, None):
            weights[idx] += (session_total - prev) * share
    return weights if sum(weights) > 0 else [1.0] * len(prompts)


def _allocate(
    session_id: str,
    cwd: Optional[str],
    totals: List[_Usage],
    prompts: List[Tuple[datetime, str]],
    weights: List[float],
    source: str,
) -> List[RequestRecord]:
    records: List[RequestRecord] = []
    for usage in totals:
        splits = {
            f: _split_int(getattr(usage, f), weights)
            for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        }
        wsum = sum(weights)
        for i, (ts, message) in enumerate(prompts):
            part = _Usage(
                model=usage.model,
                nano_aiu=usage.nano_aiu * weights[i] / wsum,
                **{f: v[i] for f, v in splits.items()},
            )
            if not part.nano_aiu and not part.input_tokens and not part.output_tokens:
                continue
            records.append(_record(
                session_id, cwd, session_id + ":" + str(i) + ":" + usage.model,
                ts, message, part, None, source, True,
            ))
    return records


def _parse_events_file(path: Path, cwd: Optional[str]) -> List[RequestRecord]:
    session_id = path.parent.name
    prompts: List[Tuple[datetime, str]] = []
    seen_interactions: Set[str] = set()
    output_hints: List[int] = []
    seen_messages: Set[str] = set()
    checkpoints: List[Tuple[datetime, float]] = []
    shutdown: Optional[Tuple[datetime, dict]] = None
    current_model: Optional[str] = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                kind = obj.get("type")
                data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
                if kind == "user.message":
                    # A resumed session replays its prompts; one interaction is one prompt.
                    interaction = data.get("interactionId")
                    if interaction:
                        if str(interaction) in seen_interactions:
                            continue
                        seen_interactions.add(str(interaction))
                    text = " ".join(str(data.get("content") or "").split())
                    prompts.append((_parse_ts(obj.get("timestamp")), text))
                    output_hints.append(0)
                elif kind == "assistant.message":
                    # Output tokens only weight the prompt the reply answers; a replayed message counts once.
                    message_id = data.get("messageId")
                    if message_id:
                        if str(message_id) in seen_messages:
                            continue
                        seen_messages.add(str(message_id))
                    if prompts:
                        output_hints[-1] += optional_int(data.get("outputTokens")) or 0
                elif kind == "session.usage_checkpoint":
                    checkpoints.append((_parse_ts(obj.get("timestamp")), float(data.get("totalNanoAiu") or 0)))
                elif kind == "session.model_change" and data.get("newModel"):
                    current_model = str(data["newModel"])
                elif kind == "session.shutdown":
                    shutdown = (_parse_ts(obj.get("timestamp")), data)
                    session_id = str(data.get("sessionId") or session_id)
    except OSError as exc:
        _log.warning("⚠️ cli_parser: cannot read %s: %s", path, exc)
        return []

    if shutdown is not None:
        end_ts, data = shutdown
        totals = []
        for model, metrics in (data.get("modelMetrics") or {}).items():
            if not isinstance(metrics, dict):
                continue
            usage = metrics.get("usage") if isinstance(metrics.get("usage"), dict) else {}
            totals.append(_Usage(
                model=str(model),
                input_tokens=optional_int(usage.get("inputTokens")),
                output_tokens=optional_int(usage.get("outputTokens")),
                cache_read_tokens=optional_int(usage.get("cacheReadTokens")),
                cache_write_tokens=optional_int(usage.get("cacheWriteTokens")),
                nano_aiu=float(metrics.get("totalNanoAiu") or 0),
            ))
        if len(prompts) > 1:
            weights = _prompt_weights(
                [p[0] for p in prompts], output_hints, checkpoints, sum(u.nano_aiu for u in totals),
            )
            return _allocate(session_id, cwd, totals, prompts, weights, "events.jsonl-allocated")
        ts, message = prompts[0] if prompts else (end_ts, "")
        return [
            _record(session_id, cwd, session_id + ":" + u.model, ts, message, u, None, "events.jsonl", False)
            for u in totals
        ]

    if checkpoints:
        usage = _Usage(model=current_model or "unknown", nano_aiu=checkpoints[-1][1])
        if not prompts:
            prompts, output_hints = [(checkpoints[-1][0], "")], [0]
        weights = _prompt_weights([p[0] for p in prompts], output_hints, checkpoints)
        return _allocate(session_id, cwd, [usage], prompts, weights, _CHECKPOINT_SOURCE)
    return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _usage_note(store_reason: str, fallback_sessions: int) -> str:
    if store_reason == "ok":
        note = "per prompt from " + cli_session_store.DB_NAME
        if fallback_sessions:
            note += "; " + str(fallback_sessions) + " session(s) from events.jsonl totals"
        return note
    return cli_session_store.DB_NAME + " " + store_reason + " — session totals from events.jsonl"


Parsed = Tuple[List[RequestRecord], Dict[str, Any]]
# ``cached(key, signature, parse)``: the read model's lookup, or a plain parse when there is none.
Cached = Callable[[str, str, Callable[[], Parsed]], Parsed]


def _parse_now(_key: str, _signature: str, parse: Callable[[], Parsed]) -> Parsed:
    return parse()


def all_records(session_state_dir: Path, cached: Cached = _parse_now) -> Tuple[List[RequestRecord], dict]:
    """Return ``(records, usage)``; ``usage`` says which source fed them and why.

    The store and each fallback ``events.jsonl`` are separate sources for ``cached``. An events
    file's signature carries the store's, because the store decides which sessions it covers.
    """
    db_path = session_state_dir.parent / cli_session_store.DB_NAME
    store_sig = repr(cli_session_store.signature(db_path))

    def parse_store() -> Parsed:
        store, reason = cli_session_store.read_store(db_path)
        if store is None:
            return [], {"reason": reason, "available": False}
        if not _store_records_cache or _store_records_cache[0][0] is not store:
            _store_records_cache[:] = [(store, _store_records(store, session_state_dir))]
        return _store_records_cache[0][1], {"reason": reason, "available": True}

    store_records, store_meta = cached("cli-store:" + str(db_path), store_sig, parse_store)
    records: List[RequestRecord] = list(store_records)
    covered = {r.session_id for r in records}

    fallback = 0
    try:
        session_dirs = [p for p in session_state_dir.iterdir() if p.is_dir()]
    except OSError as exc:
        _log.warning("⚠️ cli_parser: cannot list %s: %s", session_state_dir, exc)
        session_dirs = []
    for sdir in session_dirs:
        events_path = sdir / "events.jsonl"
        if sdir.name in covered:
            continue
        try:
            st = events_path.stat()
        except OSError:
            continue
        parsed, _meta = cached(
            "cli-events:" + str(events_path),
            repr((st.st_mtime, st.st_size, store_sig)),
            lambda sdir=sdir, events_path=events_path: (_events_records(events_path, sdir), {}),
        )
        if parsed:
            fallback += 1
        records.extend(parsed)

    usage = {
        "usage_source": "session-store" if store_meta.get("available") else "events.jsonl",
        "usage_note": _usage_note(str(store_meta.get("reason") or ""), fallback),
    }
    return records, usage


def _events_records(events_path: Path, sdir: Path) -> List[RequestRecord]:
    parsed = _cache.get(events_path)
    if parsed is None:
        parsed = _parse_events_file(events_path, _read_cwd(sdir))
        _cache.put(events_path, parsed)
    return parsed
