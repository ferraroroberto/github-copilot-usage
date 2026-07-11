@echo off
chcp 65001 >nul
REM ============================================================================
REM  github-copilot-usage — optional system-tray launcher (Windows)
REM
REM    tray.bat            start the tray (no-op if it is already running)
REM    tray.bat --restart  stop the running tray + server, start fresh
REM
REM  Self-bootstrapping: works directly on a fresh clone (creates .venv and
REM  installs core + tray dependencies on first run — no start.bat needed).
REM  The detect -> kill -> start -> verify lifecycle lives in
REM  scripts\tray_lifecycle.ps1, invoked with -File (inline -Command output
REM  capture breaks under nested non-interactive callers). All process
REM  matching is scoped to THIS repo's .venv path, so no unrelated
REM  python/pythonw process is ever touched.
REM  Put a shortcut to this file in shell:startup for an always-on tray.
REM ============================================================================
setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [setup] creating virtual environment...
    python -m venv .venv || (
        echo [error] Python 3.9+ is required and must be on PATH.
        exit /b 1
    )
    echo [setup] installing dependencies...
    "%VENV_PY%" -m pip install --quiet --upgrade pip
    "%VENV_PY%" -m pip install --quiet -r requirements.txt || (
        echo [error] dependency install failed - check your network/proxy.
        exit /b 1
    )
)

"%VENV_PY%" -c "import pystray" 2>nul || (
    echo [setup] installing tray extras...
    "%VENV_PY%" -m pip install --quiet -r requirements-tray.txt || exit /b 1
)

REM PowerShell -File swallows args that start with "-", so translate the flag
REM to a bare word the script can receive positionally.
set "ACTION="
if /I "%~1"=="--restart" set "ACTION=restart"
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\tray_lifecycle.ps1" %ACTION%
exit /b %ERRORLEVEL%
