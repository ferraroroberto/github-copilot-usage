"""Configuration for github-copilot-usage.

Everything lives in a single ``config.json`` at the repo root (created on
first save from the defaults below; ``config.example.json`` documents the
shape). Secrets (the optional GitHub billing PAT) live in ``.env``, never in
config.json and never committed.

No third-party config libraries: a tiny hand-rolled ``.env`` loader keeps the
dependency footprint at fastapi + uvicorn + httpx, which matters on locked-down
corporate machines.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List

from src import model_labels

_log = logging.getLogger(__name__)

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
CONFIG_PATH: Path = PROJECT_ROOT / "config.json"
ENV_PATH: Path = PROJECT_ROOT / ".env"

DEFAULTS: Dict[str, Any] = {
    # HTTP port for the dashboard (loopback only).
    "port": 8377,
    # Your monthly credit allowance. Copy the total from VS Code's Copilot
    # menu ("Credits X / Y used" -> Y): plan defaults are Business 300,
    # Enterprise 1000, Pro 300, Pro+ 1500, but org-provisioned credit pools
    # can be much larger (e.g. 23400).
    "monthly_credits": 300,
    # Day of month your billing cycle resets (GitHub resets on the 1st UTC).
    "cycle_reset_day": 1,
    # Extra storage roots to scan, for VS Code variants this tool does not
    # auto-discover (e.g. a company-branded fork). Each entry can point at the
    # product dir (…/MyEditor), its User dir, or a workspaceStorage dir.
    "extra_roots": [],
    # Feature blocks switched off on this machine: {"<block id>": false}. A block that is not listed is on;
    # src/features.py names the blocks.
    "features": {},
    # Models to merge: {"<normalised name>": "<normalised name it merges into>"}. Exact names
    # only; src/model_labels.py says what normalising removes.
    "model_aliases": {},
    # Chart colour per model: {"<normalised name>": {"family": "openai", "opacity": 0.6}}.
    # Either field may be left out; the family defaults to the model's provider.
    "model_colors": {},
    # First local day (YYYY-MM-DD) that counts; earlier records are left out of every total.
    # Empty means every record counts.
    "telemetry_start_date": "",
    # Custom session names: {"<session id>": "<name>"}. Stored here, never in Copilot's own files.
    "session_names": {},
}

# Editable via POST /api/config — everything else requires editing the file.
_UI_EDITABLE_KEYS = {
    "monthly_credits", "cycle_reset_day", "extra_roots", "features",
    "model_aliases", "model_colors", "telemetry_start_date", "session_names",
}

MAX_SESSION_NAME = 120


def validate_start_date(value: Any) -> str:
    """``""`` or a calendar date as YYYY-MM-DD; anything else raises ValueError."""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError("must be a date like 2026-01-31, or empty") from None


def clean_session_name(value: Any) -> str:
    """One custom name with its whitespace collapsed; empty means no name."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("must be text")
    name = " ".join(value.split())
    if len(name) > MAX_SESSION_NAME:
        raise ValueError("at most %d characters" % MAX_SESSION_NAME)
    return name


def validate_session_names(value: Any) -> Dict[str, str]:
    """The ``{session id: name}`` map with empty names dropped; a malformed entry raises ValueError."""
    if not isinstance(value, dict):
        raise ValueError("must be an object of session id to name")
    cleaned: Dict[str, str] = {}
    for session_id, name in value.items():
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("a session id must be non-empty text")
        text = clean_session_name(name)
        if text:
            cleaned[session_id] = text
    return cleaned


def validate_features(value: Any) -> Dict[str, bool]:
    """The ``{block id: on}`` map; a malformed entry raises ValueError."""
    if not isinstance(value, dict):
        raise ValueError("must be an object of block id to true or false")
    cleaned: Dict[str, bool] = {}
    for block_id, on in value.items():
        if not isinstance(block_id, str) or not block_id:
            raise ValueError("a block id must be non-empty text")
        if not isinstance(on, bool):
            raise ValueError("%s must be true or false" % block_id)
        cleaned[block_id] = on
    return cleaned


# Keys whose value has a shape beyond a number or a flag; each raises ValueError when malformed.
_VALIDATORS: Dict[str, Callable[[Any], Any]] = {
    "model_aliases": model_labels.validate_aliases,
    "model_colors": model_labels.validate_colors,
    "telemetry_start_date": validate_start_date,
    "features": validate_features,
    "session_names": validate_session_names,
}


class ConfigError(ValueError):
    """A settings update the server refuses; the message says which key and why."""

_lock = threading.Lock()
_cache: Dict[str, Any] = {}
_loaded = False


def _load_env_file(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, no interpolation."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load() -> Dict[str, Any]:
    """Return the merged config (defaults <- config.json <- env overrides)."""
    global _loaded
    with _lock:
        if _loaded:
            return copy.deepcopy(_cache)
        _load_env_file(ENV_PATH)
        merged: Dict[str, Any] = copy.deepcopy(DEFAULTS)
        cli_was_off = False
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # The Copilot CLI flag of older files is now the "copilot_cli" feature block.
                cli_was_off = raw.pop("include_copilot_cli", True) is False
                for k, v in raw.items():
                    if k in DEFAULTS:
                        merged[k] = v
                    else:
                        _log.warning("⚠️ config: ignoring unknown key %r in %s", k, CONFIG_PATH)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            _log.warning("⚠️ config: %s unreadable (%s); using defaults", CONFIG_PATH, exc)

        for k, check in _VALIDATORS.items():
            try:
                merged[k] = check(merged[k])
            except ValueError as exc:
                _log.warning("⚠️ config: ignoring %s in %s (%s)", k, CONFIG_PATH, exc)
                merged[k] = copy.deepcopy(DEFAULTS[k])
        if cli_was_off:
            merged["features"].setdefault("copilot_cli", False)

        port_env = os.environ.get("COPILOT_USAGE_PORT")
        if port_env:
            try:
                merged["port"] = int(port_env)
            except ValueError:
                _log.warning("⚠️ config: ignoring non-numeric COPILOT_USAGE_PORT=%r", port_env)

        _cache.clear()
        _cache.update(merged)
        _loaded = True
        return copy.deepcopy(_cache)


def _clean(key: str, value: Any) -> Any:
    """One UI-editable value in its stored form; raises ConfigError when it cannot be."""
    try:
        if key == "monthly_credits":
            return max(0, int(value))
        if key == "cycle_reset_day":
            return min(28, max(1, int(value)))
        if key == "extra_roots":
            if not isinstance(value, list):
                raise ValueError("must be a list of paths")
            return [str(p) for p in value if str(p).strip()]
        return _VALIDATORS[key](value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("%s: %s" % (key, exc)) from exc


def save(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Persist UI-editable keys to config.json and return the new config.

    Every update is checked before any is written, so a refused request changes nothing.
    """
    unknown = sorted(k for k in updates if k not in DEFAULTS)
    if unknown:
        raise ConfigError("unknown setting(s): %s" % ", ".join(unknown))
    readonly = sorted(k for k in updates if k not in _UI_EDITABLE_KEYS)
    if readonly:
        raise ConfigError("not editable here (edit config.json): %s" % ", ".join(readonly))
    cleaned = {k: _clean(k, v) for k, v in updates.items()}

    current = load()
    with _lock:
        current.update(cleaned)
        on_disk = {k: current[k] for k in DEFAULTS}
        CONFIG_PATH.write_text(
            json.dumps(on_disk, indent=2) + "\n", encoding="utf-8"
        )
        _cache.clear()
        _cache.update(current)
    return copy.deepcopy(current)


def feature_enabled(block_id: str) -> bool:
    """Whether a feature block is on for this machine; a block that is not listed is on."""
    return bool(load()["features"].get(block_id, True))


def extra_roots() -> List[Path]:
    return [Path(p).expanduser() for p in load().get("extra_roots", [])]
