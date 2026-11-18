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
   ``user.message`` prompts and marked ``usage_estimated`` — weighted by each
   prompt's checkpoint delta when checkpoints exist, evenly otherwise. A single
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
from typing import Dict, List, Optional, Set, Tuple

from src import cli_session_store
from src.cli_session_store import StoreSnapshot
from src.vscode_parser import RequestRecord, _MtimeCache

_log = logging.getLogger(__name__)

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
    """Token and credit totals for one model; ``input_tokens`` includes cache traffic."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    nano_aiu: float = 0.0


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
    uncached = max(usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens, 0)
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
        prompt_tokens=uncached + usage.cache_write_tokens,
        completion_tokens=usage.output_tokens,
        credits=usage.nano_aiu / 1e9,
        elapsed_ms=elapsed_ms,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        usage_source=usage_source,
        usage_estimated=estimated,
    )


# ---------------------------------------------------------------------------
# session-store.db: exact per-prompt records
# ---------------------------------------------------------------------------


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
            input_tokens=sum(c.input_tokens for c in calls),
            output_tokens=sum(c.output_tokens for c in calls),
            cache_read_tokens=sum(c.cache_read_tokens for c in calls),
            cache_write_tokens=sum(c.cache_write_tokens for c in calls),
            nano_aiu=float(sum(c.nano_aiu for c in calls)),
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


def _split_int(total: int, weights: List[float]) -> List[int]:
    """Split ``total`` by ``weights`` into integers that still sum to ``total``."""
    wsum = sum(weights)
    raw = [total * w / wsum for w in weights]
    parts = [int(x) for x in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - parts[i], reverse=True)
    for i in order[: total - sum(parts)]:
        parts[i] += 1
    return parts


def _prompt_weights(prompts: List[datetime], checkpoints: List[Tuple[datetime, float]]) -> List[float]:
    """Each checkpoint's credit delta goes to the latest prompt at or before it."""
    weights = [0.0] * len(prompts)
    prev = 0.0
    for ts, total in checkpoints:
        idx = max((i for i, p in enumerate(prompts) if p <= ts), default=0)
        weights[idx] += max(total - prev, 0.0)
        prev = max(prev, total)
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
            usage = metrics.get("usage") or {}
            totals.append(_Usage(
                model=str(model),
                input_tokens=int(usage.get("inputTokens") or 0),
                output_tokens=int(usage.get("outputTokens") or 0),
                cache_read_tokens=int(usage.get("cacheReadTokens") or 0),
                cache_write_tokens=int(usage.get("cacheWriteTokens") or 0),
                nano_aiu=float(metrics.get("totalNanoAiu") or 0),
            ))
        if len(prompts) > 1:
            return _allocate(session_id, cwd, totals, prompts, _prompt_weights([p[0] for p in prompts], checkpoints), "events.jsonl-allocated")
        ts, message = prompts[0] if prompts else (end_ts, "")
        return [
            _record(session_id, cwd, session_id + ":" + u.model, ts, message, u, None, "events.jsonl", False)
            for u in totals
        ]

    if checkpoints:
        usage = _Usage(model=current_model or "unknown", nano_aiu=checkpoints[-1][1])
        if not prompts:
            prompts = [(checkpoints[-1][0], "")]
        return _allocate(session_id, cwd, [usage], prompts, _prompt_weights([p[0] for p in prompts], checkpoints), "events.jsonl-checkpoint")
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


def all_records(session_state_dir: Path) -> Tuple[List[RequestRecord], dict]:
    """Return ``(records, usage)``; ``usage`` says which source fed them and why."""
    store, reason = cli_session_store.read_store(session_state_dir.parent / cli_session_store.DB_NAME)
    records: List[RequestRecord] = []
    if store is not None:
        if not _store_records_cache or _store_records_cache[0][0] is not store:
            _store_records_cache[:] = [(store, _store_records(store, session_state_dir))]
        records.extend(_store_records_cache[0][1])
    covered = {r.session_id for r in records}

    fallback = 0
    try:
        session_dirs = [p for p in session_state_dir.iterdir() if p.is_dir()]
    except OSError as exc:
        _log.warning("⚠️ cli_parser: cannot list %s: %s", session_state_dir, exc)
        session_dirs = []
    for sdir in session_dirs:
        events_path = sdir / "events.jsonl"
        if sdir.name in covered or not events_path.is_file():
            continue
        parsed = _cache.get(events_path)
        if parsed is None:
            parsed = _parse_events_file(events_path, _read_cwd(sdir))
            _cache.put(events_path, parsed)
        if parsed:
            fallback += 1
        records.extend(parsed)

    usage = {
        "usage_source": "session-store" if store is not None else "events.jsonl",
        "usage_note": _usage_note(reason, fallback),
    }
    return records, usage
