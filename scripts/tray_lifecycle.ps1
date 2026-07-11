# tray_lifecycle.ps1 — detect / stop / start / verify the tray, in one place.
#
# Called by tray.bat via `powershell -File` (never inline -Command: cmd-side
# `for /f` capture of inline PowerShell output mangles quoting under nested
# non-interactive callers). Also fine to run directly:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\tray_lifecycle.ps1
#   powershell ... -File scripts\tray_lifecycle.ps1 --restart
#
# Safety: every process match is scoped to THIS repo's .venv path AND the
# `app.tray` module in the command line, and always excludes the current
# process — no unrelated python/pythonw is ever touched.

param([string]$Action = "")

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$VenvDir = Join-Path $RepoDir ".venv"
$Pythonw = Join-Path $VenvDir "Scripts\pythonw.exe"

if (-not (Test-Path $Pythonw)) {
    Write-Output "[error] $Pythonw not found - run tray.bat (or start.bat) to bootstrap first."
    exit 1
}

# Resolve the port (config.json override, else the 8377 default).
$Port = 8377
$ConfigPath = Join-Path $RepoDir "config.json"
if (Test-Path $ConfigPath) {
    try {
        $cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
        if ($cfg.port) { $Port = [int]$cfg.port }
    } catch { }
}
$HealthUrl = "http://127.0.0.1:$Port/health"

function Get-TrayProcesses {
    $esc = [regex]::Escape($VenvDir)
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -and
        $_.CommandLine -match $esc -and
        $_.CommandLine -match "app\.tray"
    }
}

$existing = @(Get-TrayProcesses)

if ($Action -match "^(--)?restart$") {
    foreach ($p in $existing) {
        Write-Output "[restart] stopping tray PID $($p.ProcessId) ..."
        # taskkill via cmd so its output (incl. "already gone" errors after a
        # /T tree kill) never becomes a terminating PowerShell error.
        cmd /c "taskkill /PID $($p.ProcessId) /T /F >nul 2>&1"
    }
    # Reclaim the port from a leftover dev server, scoped to this repo's venv.
    try {
        $owners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique
    } catch { $owners = @() }
    $esc = [regex]::Escape($VenvDir)
    foreach ($ownerPid in $owners) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerPid" -ErrorAction SilentlyContinue
        if ($proc -and $proc.CommandLine -match $esc) {
            Write-Output "[restart] reclaiming port $Port from PID $ownerPid ..."
            cmd /c "taskkill /PID $ownerPid /T /F >nul 2>&1"
        }
    }
    Start-Sleep -Milliseconds 500
} elseif ($existing.Count -gt 0) {
    Write-Output "[ok] tray already running - PID $($existing[0].ProcessId) - $HealthUrl"
    exit 0
}

# If the port is still taken at this point it belongs to someone else — say
# so precisely instead of starting a tray whose server thread cannot bind.
try {
    $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
} catch { $busy = $null }
if ($busy) {
    $ownerPid = ($busy | Select-Object -First 1).OwningProcess
    Write-Output "[error] port $Port is in use by PID $ownerPid (not this app's tray). Stop it or change 'port' in config.json."
    exit 1
}

Start-Process -FilePath $Pythonw -ArgumentList "-m", "app.tray" -WorkingDirectory $RepoDir -WindowStyle Hidden

# Verify: poll /health up to ~10 s so success means "serving", not "spawned".
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
        $ver = (ConvertFrom-Json $resp.Content).version
        Write-Output "[ok] tray running - dashboard at http://127.0.0.1:$Port/ (v$ver)"
        exit 0
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
Write-Output "[error] tray process started but $HealthUrl did not come up within 10s."
exit 1
