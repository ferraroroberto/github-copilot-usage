"""Parser for GitHub Copilot CLI session logs (optional source).

The Copilot CLI writes ``~/.copilot/session-state/<uuid>/``. Every session
gets a ``workspace.yaml`` (cwd metadata); only *clean-shutdown* sessions also
get an ``events.jsonl`` whose ``session.shutdown`` event carries exact
per-model credit/token totals (``totalNanoAiu``: nano AI-usage units;
credits = nanoAiu / 1e9). Sessions still in flight, or that crashed, have no
``events.jsonl`` and are skipped.

Granularity is per-session x per-model (the CLI does not expose a per-turn
breakdown), so these records have no message text / mode / prompt details.

``workspace.yaml`` is trivially flat, so the single ``cwd:`` line is read
with a string scan instead of pulling in a YAML dependency.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.vscode_parser import RequestRecord, _MtimeCache

_log = logging.getLogger(__name__)

_cache = _MtimeCache()


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


def _parse_ts(raw: Optional[str]) -> datetime:
    try:
        return datetime.fromisoformat(str(raw).rstrip("Z")).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(tz=timezone.utc)


def _parse_events_file(path: Path, cwd: Optional[str]) -> List[RequestRecord]:
    project_path = cwd
    project_name = (
        cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if cwd else "(unknown)"
    )
    project_key = (cwd or "copilot-cli").lower()

    records: List[RequestRecord] = []
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
                if obj.get("type") != "session.shutdown":
                    continue

                data = obj.get("data") or {}
                session_id = str(data.get("sessionId") or path.parent.name)
                ts = _parse_ts(obj.get("timestamp"))
                model_metrics = data.get("modelMetrics") or {}
                for model, metrics in model_metrics.items():
                    if not isinstance(metrics, dict):
                        continue
                    usage = metrics.get("usage") or {}
                    nano_aiu = metrics.get("totalNanoAiu") or 0
                    records.append(
                        RequestRecord(
                            ide="Copilot CLI",
                            source="cli",
                            project_key=project_key,
                            project_name=project_name,
                            project_path=project_path,
                            session_id=session_id,
                            session_title="CLI session — " + project_name,
                            request_id=session_id + ":" + str(model),
                            ts=ts,
                            message="",
                            mode="cli",
                            model_requested=str(model),
                            model=str(model),
                            prompt_tokens=int(usage.get("inputTokens") or 0),
                            completion_tokens=int(usage.get("outputTokens") or 0),
                            credits=float(nano_aiu) / 1e9,
                            elapsed_ms=None,
                        )
                    )
    except OSError as exc:
        _log.warning("⚠️ cli_parser: cannot read %s: %s", path, exc)
    return records


def all_records(session_state_dir: Path) -> List[RequestRecord]:
    records: List[RequestRecord] = []
    try:
        session_dirs = [p for p in session_state_dir.iterdir() if p.is_dir()]
    except OSError as exc:
        _log.warning("⚠️ cli_parser: cannot list %s: %s", session_state_dir, exc)
        return records

    for sdir in session_dirs:
        events_path = sdir / "events.jsonl"
        if not events_path.is_file():
            continue
        cached = _cache.get(events_path)
        if cached is not None:
            records.extend(cached)
            continue
        parsed = _parse_events_file(events_path, _read_cwd(sdir))
        _cache.put(events_path, parsed)
        records.extend(parsed)
    return records
