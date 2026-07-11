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

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

_log = logging.getLogger(__name__)

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
CONFIG_PATH: Path = PROJECT_ROOT / "config.json"
ENV_PATH: Path = PROJECT_ROOT / ".env"

DEFAULTS: Dict[str, Any] = {
    # HTTP port for the dashboard (loopback only).
    "port": 8377,
    # Your plan's monthly premium-request / AI-credit allowance.
    # Copilot Business = 300, Enterprise = 1000, Pro = 300, Pro+ = 1500.
    "monthly_credits": 300,
    # Day of month your billing cycle resets (GitHub resets on the 1st UTC).
    "cycle_reset_day": 1,
    # Extra storage roots to scan, for VS Code variants this tool does not
    # auto-discover (e.g. a company-branded fork). Each entry can point at the
    # product dir (…/MyEditor), its User dir, or a workspaceStorage dir.
    "extra_roots": [],
    # Also parse GitHub Copilot CLI sessions from ~/.copilot/session-state.
    "include_copilot_cli": True,
}

# Editable via POST /api/config — everything else requires editing the file.
_UI_EDITABLE_KEYS = {"monthly_credits", "cycle_reset_day", "extra_roots", "include_copilot_cli"}

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
            return dict(_cache)
        _load_env_file(ENV_PATH)
        merged: Dict[str, Any] = dict(DEFAULTS)
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in DEFAULTS:
                        merged[k] = v
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            _log.warning("⚠️ config: %s unreadable (%s); using defaults", CONFIG_PATH, exc)

        port_env = os.environ.get("COPILOT_USAGE_PORT")
        if port_env:
            try:
                merged["port"] = int(port_env)
            except ValueError:
                _log.warning("⚠️ config: ignoring non-numeric COPILOT_USAGE_PORT=%r", port_env)

        _cache.clear()
        _cache.update(merged)
        _loaded = True
        return dict(_cache)


def save(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Persist UI-editable keys to config.json and return the new config."""
    current = load()
    with _lock:
        for k, v in updates.items():
            if k not in _UI_EDITABLE_KEYS:
                continue
            if k == "monthly_credits":
                v = max(0, int(v))
            elif k == "cycle_reset_day":
                v = min(28, max(1, int(v)))
            elif k == "extra_roots":
                if not isinstance(v, list):
                    continue
                v = [str(p) for p in v if str(p).strip()]
            elif k == "include_copilot_cli":
                v = bool(v)
            current[k] = v
        on_disk = {k: current[k] for k in DEFAULTS}
        CONFIG_PATH.write_text(
            json.dumps(on_disk, indent=2) + "\n", encoding="utf-8"
        )
        _cache.clear()
        _cache.update(current)
    return dict(current)


def extra_roots() -> List[Path]:
    return [Path(p).expanduser() for p in load().get("extra_roots", [])]
