@echo off
chcp 65001 >nul
REM ============================================================================
REM  github-copilot-usage — optional system-tray launcher (Windows)
REM
REM    tray.bat            start the tray (no-op if it is already running)
REM    tray.bat --restart  stop the running tray + server, start fresh
REM
REM  Detection and kill are scoped to THIS repo's .venv path in the process
REM  command line, so no unrelated python/pythonw process is ever touched.
REM  Put a shortcut to this file in shell:startup for an always-on tray.
REM ============================================================================
setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "VENV_PYW=%SCRIPT_DIR%.venv\Scripts\pythonw.exe"

if not exist "%VENV_PY%" (
    echo [setup] run start.bat once first to create the environment.
    exit /b 1
)

"%VENV_PY%" -c "import pystray" 2>nul || (
    echo [setup] installing tray extras...
    "%VENV_PY%" -m pip install --quiet -r requirements-tray.txt || exit /b 1
)

REM --- find a running tray owned by this repo (venv path + app.tray in cmdline)
set "PS=C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
for /f "usebackq delims=" %%P in (`"%PS%" -NoProfile -NonInteractive -Command ^
  "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match [regex]::Escape('%SCRIPT_DIR%.venv') -and $_.CommandLine -match 'app\.tray' } | Select-Object -First 1 -ExpandProperty ProcessId)"`) do set "TRAY_PID=%%P"

if "%~1"=="--restart" (
    if defined TRAY_PID (
        echo [restart] stopping tray PID %TRAY_PID% ...
        taskkill /PID %TRAY_PID% /T /F >nul 2>&1
    )
) else (
    if defined TRAY_PID (
        echo [ok] tray already running - PID %TRAY_PID%
        exit /b 0
    )
)

start "" "%VENV_PYW%" -m app.tray
echo [ok] tray started.
