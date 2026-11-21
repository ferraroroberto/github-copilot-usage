"""The tray restart finds its own process under either spelling of the fleet root.

`E:\\automation` is a junction to `C:\\automation`, so one checkout has two paths and a
tray started from one carries it in its command line. `Test-UnderVenv` (in
`scripts/tray_match.ps1`, the match `tray_lifecycle.ps1` uses to detect and to
reclaim) is run for real over synthetic command lines; a sibling checkout and another
repo must stay unmatched so the restart never touches them. `/health` carries the
build sha so a restart is confirmed by build, not by process start time.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import pytest
from fastapi.testclient import TestClient

from app import build_info
from app.server import app

ROOT = Path(__file__).resolve().parent.parent
MATCH = ROOT / "scripts" / "tray_match.ps1"
POWERSHELL = shutil.which("powershell")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

windows_only = pytest.mark.skipif(
    sys.platform != "win32" or not POWERSHELL,
    reason="the tray match is a Windows PowerShell script",
)

VENV = r"C:\automation\some-repo\.venv"
TRAY = r' -m app.tray'

# (command line, expected match)
CASES: List[Tuple[str, bool]] = [
    (r'"C:\automation\some-repo\.venv\Scripts\pythonw.exe"' + TRAY, True),
    (r'"E:\automation\some-repo\.venv\Scripts\pythonw.exe"' + TRAY, True),
    (r'C:/automation/some-repo/.venv/Scripts/pythonw.exe' + TRAY, True),
    (r'e:/Automation/Some-Repo/.venv/Scripts/pythonw.exe' + TRAY, True),
    (r'"C:\automation\some-repo-wt-228\.venv\Scripts\pythonw.exe"' + TRAY, False),
    (r'"C:\automation\other-repo\.venv\Scripts\pythonw.exe"' + TRAY, False),
    (r'"C:\elsewhere\automation\some-repo\.venv\Scripts\pythonw.exe"' + TRAY, False),
    (r'"C:\automation\some-repo\.venv-old\Scripts\pythonw.exe"' + TRAY, False),
    ("", False),
]


def _match_all(venv_dir: str) -> List[bool]:
    lines = "\n".join("'" + cmd.replace("'", "''") + "'" for cmd, _ in CASES)
    script = (
        '$ErrorActionPreference = "Stop"\n'
        f'. "{MATCH}"\n'
        f"$cases = @(\n{lines}\n)\n"
        f"foreach ($c in $cases) {{ Write-Output (Test-UnderVenv $c '{venv_dir}') }}\n"
    )
    run = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=60,
    )
    assert run.returncode == 0, run.stderr
    return [line.strip() == "True" for line in run.stdout.splitlines() if line.strip()]


@windows_only
@pytest.mark.parametrize(
    "venv_dir",
    [VENV, r"E:\automation\some-repo\.venv", "C:/automation/some-repo/.venv", "C:\\automation\\some-repo\\.venv\\"],
)
def test_match_ignores_the_spelling_of_the_caller_and_the_process(venv_dir: str) -> None:
    assert _match_all(venv_dir) == [expected for _, expected in CASES]


def test_health_carries_the_build_sha() -> None:
    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["git_sha"] == build_info.GIT_SHA
