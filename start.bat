@echo off
chcp 65001 >nul
REM ============================================================================
REM  github-copilot-usage — one-command start (Windows)
REM  Creates .venv on first run, installs dependencies, starts the dashboard
REM  and opens it in your browser. Re-run any time; setup steps are skipped
REM  when already done.
REM ============================================================================
setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] creating virtual environment...
    python -m venv .venv || (
        echo [error] Python 3.9+ is required and must be on PATH.
        exit /b 1
    )
    echo [setup] installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (
        echo [error] dependency install failed - check your network/proxy.
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m app --open %*
