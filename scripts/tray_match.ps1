# tray_match.ps1 - does a process command line belong to this repo's .venv?
#
# Dot-sourced by tray_lifecycle.ps1 and by the test that feeds it synthetic
# command lines. The fleet root is reachable under two spellings (a drive
# letter and its junction target) and a process may carry either one, with
# either slash style, so a plain substring test of the caller's spelling
# misses a tray started from the other. Both sides are folded to one shape
# (backslashes, lower case, drive letter dropped) before comparing.

function Get-VenvTail {
    param([string]$VenvDir)
    $tail = ($VenvDir -replace "/", "\").TrimEnd("\").ToLowerInvariant()
    $tail = $tail -replace "^[a-z]:", ""
    return $tail + "\"
}

function Test-UnderVenv {
    param([string]$CommandLine, [string]$VenvDir)
    if (-not $CommandLine) { return $false }
    $line = ($CommandLine -replace "/", "\").ToLowerInvariant()
    # A drive letter must sit directly before the tail: a sibling checkout
    # (<repo>-wt-N) or an unrelated parent folder cannot share the prefix.
    $pattern = "(^|[^a-z0-9])[a-z]:" + [regex]::Escape((Get-VenvTail $VenvDir))
    return [bool]($line -match $pattern)
}
