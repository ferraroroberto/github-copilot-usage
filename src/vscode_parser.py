"""Parser for VS Code Copilot Chat session logs.

Each session is an event-sourced ``<uuid>.jsonl``: the first line (kind 0) is
a full snapshot ``{"kind":0,"v":{...session...}}``; later lines patch it —
``{"kind":2,"k":["requests"],"i":N,"v":{...}}`` inserts a request,
``{"kind":1,"k":["requests",N,"field",...],"v":...}`` sets a (possibly
nested) field. We replay just enough of the stream to reconstruct the
``requests`` array plus the session metadata the dashboard needs; response
text deltas and tool-call payloads are deliberately not replayed.

Per completed request the extension records **exact billing data** (not an
estimate): ``copilotCredits`` (1 credit = 1 premium request unit = $0.01 on
GitHub's AI-credits model), ``promptTokens`` / ``completionTokens``, a
``promptTokenDetails`` composition breakdown (System Instructions / Tool
Definitions / Messages %), the resolved model, and ``elapsedMs``.

Read-only, zero subprocesses, mtime-cached: a file is re-parsed only when its
mtime or size changes, so the 30 s dashboard poll is cheap.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

_log = logging.getLogger(__name__)

# Top-level session-snapshot fields we keep.
_SESSION_META_FIELDS = ("sessionId", "customTitle", "creationDate")

# A context label is a name, not a document: the cap bounds its length, and the list stays
# shallow so one request with a huge attachment set cannot bloat the cached records.
CONTEXT_LABEL_MAX = 160
_CONTEXT_ITEMS_MAX = 100


@dataclass
class RequestRecord:
    """One Copilot Chat request (one user turn) with exact billing data."""

    ide: str
    source: str                 # "vscode" | "cli"
    project_key: str            # stable grouping key (lowercased path or pseudo-key)
    project_name: str           # short display name
    project_path: Optional[str]
    session_id: str
    session_title: str
    request_id: str
    ts: datetime
    message: str                # first chars of the user prompt
    mode: str                   # agent | ask | edit | … ("" when unknown)
    model_requested: str
    model: str                  # resolved model (display string)
    prompt_tokens: Optional[int]        # None = the source did not record the figure (not zero)
    completion_tokens: Optional[int]
    credits: Optional[float]    # None = request never got billing data
    elapsed_ms: Optional[int]
    prompt_details: List[dict] = field(default_factory=list)
    # Names of what rode along in the prompt (attached files, tools, other attachments): labels
    # only, never their content.
    context_items: List[str] = field(default_factory=list)
    error: bool = False
    # prompt_tokens is the comparable input volume: uncached input plus
    # cache-write. Cache-read is kept apart so it never inflates that volume. VS Code
    # logs no cache traffic, so its records keep 0; None marks a CLI figure not recorded.
    cache_read_tokens: Optional[int] = 0
    cache_write_tokens: Optional[int] = 0
    # vscode | session-store | events.jsonl | events.jsonl-allocated | events.jsonl-checkpoint
    # | unavailable (the CLI recorded no input or output tokens for it)
    usage_source: str = ""
    usage_estimated: bool = False   # True = allocated from session/checkpoint totals, not measured per prompt
    # Set only on the read-time copy of a renamed session: the title the log itself carried.
    parsed_title: str = ""


@dataclass
class _CacheEntry:
    mtime: float
    size: int
    records: List[RequestRecord]


class _MtimeCache:
    def __init__(self) -> None:
        self._entries: Dict[str, _CacheEntry] = {}

    def get(self, path: Path) -> Optional[List[RequestRecord]]:
        try:
            st = path.stat()
        except OSError:
            return []
        hit = self._entries.get(str(path))
        if hit is not None and hit.mtime == st.st_mtime and hit.size == st.st_size:
            return hit.records
        return None

    def put(self, path: Path, records: List[RequestRecord]) -> None:
        try:
            st = path.stat()
        except OSError:
            return
        self._entries[str(path)] = _CacheEntry(st.st_mtime, st.st_size, records)

    def clear(self) -> None:
        self._entries.clear()


_file_cache = _MtimeCache()
_workspace_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}


def clear_caches() -> None:
    """Drop every parsed file and resolved workspace, so the next scan re-reads from disk."""
    _file_cache.clear()
    _workspace_cache.clear()


# ---------------------------------------------------------------------------
# Workspace (project) resolution
# ---------------------------------------------------------------------------


def decode_file_uri(uri: str) -> Optional[str]:
    """Decode a ``file://`` URI into a native filesystem path (win + posix)."""
    try:
        parsed = urlparse(uri)
    except ValueError:
        return None
    if parsed.scheme not in ("file", "vscode-remote") or not parsed.path:
        return None
    raw = unquote(parsed.path)
    if parsed.scheme == "vscode-remote":
        return raw  # keep the remote path verbatim, it is still a useful label
    # Windows: "/c:/projects/repo" -> "C:\projects\repo"
    stripped = raw.lstrip("/")
    if len(stripped) >= 2 and stripped[1] == ":":
        return stripped[0].upper() + stripped[1:].replace("/", "\\")
    return raw


def project_from_workspace_dir(workspace_dir: Optional[Path]) -> Tuple[str, str, Optional[str]]:
    """Resolve a workspaceStorage hash dir into (key, name, path).

    Reads the dir's ``workspace.json`` (``{"folder": uri}`` or
    ``{"workspace": uri}``). A ``.code-workspace`` file maps to its own
    basename so multi-root workspaces still read as one project.
    """
    if workspace_dir is None:
        return "(no workspace)", "(no workspace)", None

    cache_key = str(workspace_dir)
    if cache_key in _workspace_cache:
        name, path = _workspace_cache[cache_key]
        return (path or name or workspace_dir.name).lower(), name or workspace_dir.name, path

    name: Optional[str] = None
    path: Optional[str] = None
    try:
        raw = json.loads((workspace_dir / "workspace.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict):
        uri = raw.get("workspace") or raw.get("folder")
        if isinstance(uri, str):
            decoded = decode_file_uri(uri)
            if decoded:
                path = decoded
                base = decoded.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
                if base.lower().endswith(".code-workspace"):
                    base = base[: -len(".code-workspace")]
                name = base

    _workspace_cache[cache_key] = (name, path)
    return (path or name or workspace_dir.name).lower(), name or workspace_dir.name, path


# ---------------------------------------------------------------------------
# JSONL replay
# ---------------------------------------------------------------------------


def _apply_set(target: Any, path: List[Any], value: Any) -> None:
    """Generic setter for a kind-1 patch path like ["requests", 3, "result"]."""
    node = target
    for seg in path[:-1]:
        if isinstance(node, list):
            if not isinstance(seg, int) or not (0 <= seg < len(node)):
                return
            node = node[seg]
        elif isinstance(node, dict):
            nxt = node.get(seg)
            if nxt is None:
                nxt = node[seg] = {}
            node = nxt
        else:
            return
    last = path[-1]
    if isinstance(node, list):
        if isinstance(last, int) and 0 <= last < len(node):
            node[last] = value
    elif isinstance(node, dict):
        node[last] = value


def replay_session(lines: List[dict]) -> Tuple[Dict[str, Any], List[dict]]:
    """Replay the patch stream into (session_meta, requests)."""
    meta: Dict[str, Any] = {}
    requests: List[dict] = []
    for obj in lines:
        kind = obj.get("kind")
        if kind == 0:
            v = obj.get("v") or {}
            for f in _SESSION_META_FIELDS:
                if v.get(f) is not None:
                    meta[f] = v[f]
            requests = [r for r in (v.get("requests") or []) if isinstance(r, dict)]
        elif kind == 2:
            if obj.get("k") == ["requests"]:
                idx, v = obj.get("i"), obj.get("v")
                if isinstance(v, dict) and isinstance(idx, int) and 0 <= idx <= len(requests):
                    requests.insert(idx, v)
        elif kind == 1:
            k = obj.get("k") or []
            if not isinstance(k, list) or not k:
                continue
            if k[0] == "requests" and len(k) >= 3:
                idx = k[1]
                if isinstance(idx, int) and 0 <= idx < len(requests):
                    _apply_set(requests[idx], k[2:], obj.get("v"))
            elif len(k) == 1 and k[0] in _SESSION_META_FIELDS:
                meta[k[0]] = obj.get("v")
    return meta, requests


# ---------------------------------------------------------------------------
# Record extraction
# ---------------------------------------------------------------------------


def _resolved_model(req: dict) -> str:
    result = req.get("result") or {}
    metadata = result.get("metadata") or {}
    model = metadata.get("resolvedModel")
    if isinstance(model, str) and model:
        return model
    # "GPT-5 mini • 0.8 credits" — the UI details string carries the model.
    details = result.get("details")
    if isinstance(details, str) and "•" in details:
        return details.split("•")[0].strip()
    requested = req.get("modelId")
    if isinstance(requested, str) and requested:
        return requested.split("/")[-1]
    return "unknown"


def optional_int(value: object) -> Optional[int]:
    """The integer a log field holds, or ``None`` when it is absent or not a number (never 0)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _message_text(req: dict, limit: int = 200) -> str:
    msg = req.get("message")
    text = msg.get("text") if isinstance(msg, dict) else msg if isinstance(msg, str) else ""
    text = " ".join(str(text or "").split())
    return text[:limit]


def _mode(req: dict) -> str:
    info = req.get("modeInfo")
    if isinstance(info, dict):
        for key in ("telemetryModeId", "kind"):
            v = info.get(key)
            if isinstance(v, str) and v:
                return v
    agent = req.get("agent")
    if isinstance(agent, dict):
        aid = str(agent.get("id") or "")
        if "editsAgent" in aid:
            return "agent"
    return ""


def _prompt_details(req: dict) -> List[dict]:
    """The logged composition entries whose percentage is a number from 0 to 100."""
    details = req.get("promptTokenDetails")
    if not isinstance(details, list):
        return []
    kept: List[dict] = []
    for d in details:
        pct = d.get("percentageOfPrompt") if isinstance(d, dict) else None
        if isinstance(pct, (int, float)) and not isinstance(pct, bool) and 0 <= pct <= 100:
            kept.append(d)
    return kept


def _variable_name(var: dict) -> str:
    """The display name of one attached variable: its name, else the basename of its path."""
    for key in ("name", "fullName"):
        v = var.get(key)
        if isinstance(v, str) and v.strip():
            return v
    value = var.get("value")
    if isinstance(value, dict):
        inner = value.get("uri")
        value = inner if isinstance(inner, (dict, str)) else value
    if isinstance(value, dict):
        value = value.get("path") or value.get("fsPath")
    if not isinstance(value, str) or "\n" in value:
        return ""  # a multi-line string is pasted text, never a name
    path = decode_file_uri(value) if "://" in value else value
    return (path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _context_items(req: dict) -> List[str]:
    """Names of the files, tools and attachments in the request's variable data; never contents."""
    data = req.get("variableData")
    variables = data.get("variables") if isinstance(data, dict) else None
    if not isinstance(variables, list):
        return []
    seen: set = set()
    out: List[str] = []
    for var in variables:
        if not isinstance(var, dict):
            continue
        label = " ".join(_variable_name(var).split())[:CONTEXT_LABEL_MAX]
        if label and label not in seen:
            seen.add(label)
            out.append(label)
            if len(out) >= _CONTEXT_ITEMS_MAX:
                break
    return out


def _has_error(req: dict) -> bool:
    result = req.get("result") or {}
    return bool(result.get("errorDetails"))


def parse_session_file(
    path: Path,
    ide: str,
    project_key: str,
    project_name: str,
    project_path: Optional[str],
) -> List[RequestRecord]:
    cached = _file_cache.get(path)
    if cached is not None:
        return cached

    lines: List[dict] = []
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
                if isinstance(obj, dict):
                    lines.append(obj)
    except OSError as exc:
        _log.warning("⚠️ vscode_parser: cannot read %s: %s", path, exc)
        return []

    meta, requests = replay_session(lines)
    session_id = str(meta.get("sessionId") or path.stem)
    title = str(meta.get("customTitle") or "").strip()

    records: List[RequestRecord] = []
    for req in requests:
        prompt_tokens = req.get("promptTokens")
        completion_tokens = req.get("completionTokens")
        credits = req.get("copilotCredits")
        # Skip requests with no usage signal at all (pending / abandoned turns
        # that never reached the model).
        if prompt_tokens is None and completion_tokens is None and credits is None:
            continue

        ts_ms = req.get("timestamp")
        try:
            ts = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            ts = datetime.now(tz=timezone.utc)

        message = _message_text(req)
        if not title and message:
            title = message[:80]

        records.append(
            RequestRecord(
                ide=ide,
                source="vscode",
                project_key=project_key,
                project_name=project_name,
                project_path=project_path,
                session_id=session_id,
                session_title=title or session_id[:8],
                request_id=str(req.get("requestId") or ""),
                ts=ts,
                message=message,
                mode=_mode(req),
                model_requested=str(req.get("modelId") or ""),
                model=_resolved_model(req),
                prompt_tokens=optional_int(prompt_tokens),
                completion_tokens=optional_int(completion_tokens),
                credits=float(credits) if credits is not None else None,
                elapsed_ms=int(req["elapsedMs"]) if isinstance(req.get("elapsedMs"), (int, float)) else None,
                prompt_details=_prompt_details(req),
                context_items=_context_items(req),
                error=_has_error(req),
                usage_source="vscode",
            )
        )

    # Backfill the session title onto every record (title may have been
    # derived from a later request's message).
    for r in records:
        r.session_title = title or r.session_title

    _file_cache.put(path, records)
    return records


def copied_request_losers(entries: List[Tuple[RequestRecord, float, str]]) -> List[bool]:
    """Flag the requests of a chat log that is reachable under a second file.

    ``entries`` holds each request with the mtime and path of the file it was read from. A request
    whose ``ide``, ``session_id`` and ``request_id`` are all non-empty and equal to another file's
    is the same request seen twice: the copy from the file with the newest mtime stays (the
    lexically smallest path on a tie) and every other file's copy is flagged ``True``. A request
    missing any part of that identity is never merged, nor are repeats inside one file.
    """
    winner: Dict[Tuple[str, str, str], Tuple[float, str]] = {}
    for rec, mtime, path in entries:
        if not (rec.ide and rec.session_id and rec.request_id):
            continue
        key = (rec.ide, rec.session_id, rec.request_id)
        best = winner.get(key)
        if best is None or (-mtime, path) < (-best[0], best[1]):
            winner[key] = (mtime, path)
    return [
        bool(rec.ide and rec.session_id and rec.request_id)
        and winner[(rec.ide, rec.session_id, rec.request_id)][1] != path
        for rec, _mtime, path in entries
    ]
