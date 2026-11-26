@echo off
chcp 65001 >nul
REM ============================================================================
REM  github-copilot-usage — optional system-tray launcher (Windows)
REM
REM    tray.bat            start the tray (no-op if it is already running)
REM    tray.bat --restart  stop the running tray + server, start fresh
REM
REM  Self-bootstrapping: works directly on a fresh clone (creates .venv and
REM  installs core + tray dependencies on first run — no scripts\start.bat needed).
REM  The detect -> kill -> start -> verify lifecycle lives in
REM  scripts\tray_lifecycle.ps1, shelled to as one script run (inline -Command
REM  output capture breaks under nested non-interactive callers), through
REM  src\ps_script.py (vendored from the scaffold): under AllSigned or
REM  Restricted it loads the script as a ScriptBlock. The machine's execution
REM  policy is never set or overridden, the launch method is printed, and a
REM  script it cannot run exits non-zero. All process
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

REM PowerShell takes an argument that starts with "-" as a parameter name, so
REM translate the flag to a bare word the script can receive positionally.
set "ACTION="
if /I "%~1"=="--restart" set "ACTION=restart"
"%VENV_PY%" -m src.ps_script "%SCRIPT_DIR%scripts\tray_lifecycle.ps1" %ACTION%
exit /b %ERRORLEVEL%
