#Requires -Version 5.1
<#
.SYNOPSIS
    Pre-ship verification gate. One pass/fail pipeline, fail-fast.

.DESCRIPTION
    Stages:
      1. byte-compile      - every .py under app/ src/ tests/ parses
      2. pytest            - the unit suite (fixtures only)
      3. structure ratchet - scripts/structure_ratchet.py: file size, function
                             length, flat directories, the src -> app layer
                             contract and the root layout against the baseline;
                             exit 3 ("not confirmed") is a failure, never a pass
                             (docs/layout.md)
      4. instruction ratchet - scripts/instruction_ratchet.py: the prompt-audit lint
                             rules and size caps over CLAUDE.md / AGENTS.md /
                             skills against the baseline; exit 3 is a failure

    Anchors to the repo root, so run it from anywhere:
        powershell -File scripts/verify-before-ship.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[FAIL] .venv not found at $py" -ForegroundColor Red
    Write-Host "       Create it and run: $py -m pip install -r requirements.txt -r requirements-dev.txt" -ForegroundColor Red
    exit 1
}

function Invoke-Stage {
    param([string]$Name, [scriptblock]$Body)
    Write-Host ""
    Write-Host ">> $Name" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
    Write-Host "[PASS] $Name" -ForegroundColor Green
}

Invoke-Stage "byte-compile"      { & $py -m compileall -q app src tests }
Invoke-Stage "pytest"            { & $py -m pytest -q }
Invoke-Stage "structure ratchet" { & $py "scripts/structure_ratchet.py" }
Invoke-Stage "instruction ratchet" { & $py "scripts/instruction_ratchet.py" }

Write-Host ""
Write-Host "[PASS] all checks green - safe to ship." -ForegroundColor Green
