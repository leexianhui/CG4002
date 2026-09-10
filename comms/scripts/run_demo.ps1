# PowerShell entry point for the demo rig. See scripts/run_demo.sh for what it actually does and
# comms/DEMO_RUNBOOK.md for how to read the output.
#
# Usage (from comms/):
#   .\scripts\run_demo.ps1 mock
#   .\scripts\run_demo.ps1 ble
#
# This is a thin wrapper that hands off to run_demo.sh under Git Bash rather than a second, parallel
# implementation. The launcher has to read the PSK from .env.relay and the bearer token from
# .env.engine and start six processes in a specific order; two copies of that logic would eventually
# disagree, and the failure mode of a launcher that starts things in the wrong order is a misleading
# "the system is hung" rather than an obvious error. One tested script, two ways to call it.
#
# Why this file exists at all: invoking a .sh from PowerShell does nothing whatsoever -- no output,
# no error, no exit code -- so the natural `scripts/run_demo.sh mock` looks like a rig that silently
# refuses to start.

param(
    [ValidateSet('mock', 'ble')]
    [string]$Mode = 'mock',

    # demo (default) shows startup, timesync, every resolved hit and the HP the phones see, plus
    # anything at WARNING or above. full adds the ~40 lines/second of per-window chatter. Both write
    # every event to logs/*.jsonl regardless -- this only filters the console.
    [ValidateSet('demo', 'full')]
    [string]$Verbosity = 'demo'
)

$ErrorActionPreference = 'Stop'

$bash = Get-Command bash -ErrorAction SilentlyContinue
if (-not $bash) {
    # Not on PATH: fall back to the standard Git for Windows install locations before giving up.
    $candidates = @(
        "$env:ProgramFiles\Git\usr\bin\bash.exe",
        "${env:ProgramFiles(x86)}\Git\usr\bin\bash.exe",
        "$env:LOCALAPPDATA\Programs\Git\usr\bin\bash.exe"
    )
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $found) {
        Write-Error "bash not found. Install Git for Windows, or run the components by hand (comms/README.md)."
        exit 1
    }
    $bashPath = $found
}
else {
    $bashPath = $bash.Source
}

# Run from comms/ regardless of where this was invoked from -- every path in run_demo.sh and in the
# configs (certs/engine.crt, configs/..., logs/) is relative to it.
Push-Location (Join-Path $PSScriptRoot '..')
try {
    # -l (login shell) is REQUIRED, not stylistic. bash.exe launched from PowerShell inherits
    # PowerShell's PATH verbatim and does not add its own /usr/bin, so unless Git's directories happen
    # to be on the Windows PATH the script starts with no coreutils at all: dirname, tr, sed, grep and
    # stdbuf are all "command not found". The first casualty is `cd "$(dirname "$0")/.."`, which fails
    # silently and leaves the script in the wrong directory, so it then reports "comms/.venv not
    # found" -- pointing at a broken install rather than at a broken PATH. A login shell sources the
    # MSYS profile that builds a correct PATH.
    & $bashPath '-lc' 'exec scripts/run_demo.sh "$1" "$2"' 'run_demo' $Mode $Verbosity
}
finally {
    Pop-Location
}
