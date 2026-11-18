"""A disposable health check of the read model, started from Settings.

A run takes an online backup of the read model with SQLite's backup API into a temporary folder and
starts ``app.health_check_run`` over that copy in a child process, so the real API is exercised with
the discovery, billing and alert blocks off and every outbound connection blocked. Nothing of the live
read model, the config file or Copilot's own files is written, and the temporary folder is removed
afterwards.

The result is one bounded JSON file in the runtime-data folder with the timing of every check and the
count of SQL statements by their first keyword. It never holds a prompt, a title, a path or a
statement. ``state`` is ``measured`` (every check ran), ``incomplete`` (the run stopped early; the
checks it finished are kept) or ``unavailable`` (there was nothing to measure); a run that could not be
confirmed is never reported as a pass.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src import config, read_model
from src.runtime_data import app_dir_env_var, runtime_db_path
from src.usage_store import APP

_log = logging.getLogger(__name__)

RESULT_FILENAME = "health_check.json"
# What a run measures: the samples taken per check and the time a check may take (its median).
SAMPLES = 3
BUDGET_MS = 1500
# A run that takes longer than this is stopped and reported as incomplete.
TIMEOUT_S = 180
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
# What a run switches off in its private config: nothing in it may read the machine or reach out.
_BLOCKS_OFF = {"copilot_cli": False, "billing_card": False, "pace_alert": False, "usage_log": False}

_lock = threading.Lock()
_running = False
_last: Optional[Dict[str, Any]] = None


def result_path() -> Path:
    config.load()  # .env overrides must count before the environment is read
    return runtime_db_path(APP, RESULT_FILENAME)


def _read_result() -> Optional[Dict[str, Any]]:
    try:
        body = json.loads(result_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def status() -> Dict[str, Any]:
    """Whether a run is in flight, and the last result (from this process, else from the file)."""
    with _lock:
        running, last = _running, _last
    return {"running": running, "result": last if last is not None else _read_result()}


def start() -> bool:
    """Begin a run in the background; False when one is already running."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True
    threading.Thread(target=_work, name="health-check", daemon=True).start()
    return True


def _work() -> None:
    global _running, _last
    try:
        result = run()
    except Exception as exc:  # a crash is a result, never a stuck "running" state
        _log.warning("⚠️ health check crashed: %s", exc, exc_info=True)
        result = _result("unavailable", reason="the check could not run (%s)" % type(exc).__name__)
    _store(result)
    with _lock:
        _last, _running = result, False


def _store(result: Dict[str, Any]) -> None:
    path = result_path()
    try:
        # runtime-write-exempt: path comes from runtime_db_path in result_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        # runtime-write-exempt: path comes from runtime_db_path in result_path()
        part.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        os.replace(str(part), str(path))
    except OSError as exc:
        _log.warning("⚠️ health check result not saved: %s", exc)


def _result(state: str, **fields: Any) -> Dict[str, Any]:
    return {
        "state": state, "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "budget_ms": BUDGET_MS, "samples": SAMPLES, "verdict": None, "reason": None,
        "dataset": None, "checks": [], **fields,
    }


def _dataset(copy: Path) -> Dict[str, int]:
    conn = read_model.open_file(copy)
    try:
        return {
            "sources": conn.execute("SELECT count(*) FROM sources").fetchone()[0],
            "requests": conn.execute("SELECT count(*) FROM records").fetchone()[0],
            "bytes": copy.stat().st_size,
        }
    finally:
        conn.close()


def _usable(copy: Path) -> Optional[str]:
    """Why the copy cannot be measured (the live file is empty or of another schema), else None."""
    conn = read_model.open_file(copy)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != read_model.SCHEMA_VERSION:
            return "the read model has another schema version; refresh the dashboard once to rebuild it"
        if conn.execute("SELECT count(*) FROM records").fetchone()[0] == 0:
            return "the read model holds no requests yet"
    except sqlite3.Error:
        return "the read model could not be read"
    finally:
        conn.close()
    return None


def _private_config(path: Path) -> None:
    """The live settings that shape a query, with every block that reads or reaches out switched off."""
    cfg = config.load()
    cfg["features"] = {**cfg.get("features", {}), **_BLOCKS_OFF}
    cfg["extra_roots"] = []
    cfg["tailnet_access"] = False
    # runtime-write-exempt: a file in this run's own temporary folder
    path.write_text(json.dumps(cfg), encoding="utf-8")


def _verdict(checks: List[Dict[str, Any]]) -> str:
    states = {c["state"] for c in checks}
    if "error" in states:
        return "fail"
    return "fail" if "over_budget" in states else "pass"


def _parse(stdout: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    checks: List[Dict[str, Any]] = []
    done: Optional[Dict[str, Any]] = None
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue  # a stray log line is not a result
        if isinstance(item, dict) and item.get("done"):
            done = item
        elif isinstance(item, dict) and "id" in item:
            checks.append({k: item.get(k) for k in (
                "id", "state", "samples_ms", "median_ms", "enforced", "statements", "failure")})
    return checks, done


def run() -> Dict[str, Any]:
    """One synchronous run; the result for ``state`` measured, incomplete or unavailable."""
    live = read_model.db_path()
    if not live.exists():
        return _result("unavailable", reason="there is no read model yet; open the dashboard once")
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="copilot-usage-health-"))
    try:
        pristine = work / "source.sqlite3"
        try:
            read_model.backup_live(pristine)
        except (sqlite3.Error, OSError) as exc:
            return _result("unavailable", reason="the read model could not be copied (%s)" % type(exc).__name__)
        problem = _usable(pristine)
        if problem:
            return _result("unavailable", reason=problem)
        dataset = _dataset(pristine)
        data = work / "data"
        data.mkdir()
        shutil.copyfile(str(pristine), str(data / read_model.DB_FILENAME))
        _private_config(work / "config.json")
        env = dict(
            os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1", COPILOT_USAGE_CONFIG=str(work / "config.json"),
            **{app_dir_env_var(APP): str(data)},
        )
        root = Path(__file__).resolve().parent.parent
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "app.health_check_run", str(pristine)],
                cwd=str(root), env=env, capture_output=True, timeout=TIMEOUT_S, creationflags=_NO_WINDOW,
            )
            stdout, code, reason = proc.stdout.decode("utf-8", "replace"), proc.returncode, None
        except subprocess.TimeoutExpired as exc:
            stdout, code = (exc.stdout or b"").decode("utf-8", "replace"), None
            reason = "the run was stopped after %d s" % TIMEOUT_S
        except OSError as exc:
            return _result("unavailable", reason="the check process could not start (%s)" % type(exc).__name__)
        checks, finished = _parse(stdout)
        elapsed = int((time.monotonic() - started) * 1000)
        if finished is None:
            reason = reason or "the check process stopped early (exit code %s)" % code
            return _result("incomplete", reason=reason, dataset=dataset, checks=checks, duration_ms=elapsed)
        return _result("measured", verdict=_verdict(checks), dataset=dataset, checks=checks, duration_ms=elapsed)
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
