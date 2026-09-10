# PowerShell wrapper for scripts/git_precommit_check.sh. See that file for what it checks and why.
#
#   .\scripts\git_precommit_check.ps1
#
# Exits 1 if anything is wrong, so it chains safely:
#   .\scripts\git_precommit_check.ps1; if ($LASTEXITCODE -eq 0) { git add -A; git commit -m "..." }
#
# WHY THIS EXISTS: `scripts/git_precommit_check.sh` typed at a PowerShell prompt produces no output,
# no error and no exit code -- PowerShell has no .sh handler. For a SECRET check that silent success
# is the worst possible failure: it looks like "clean, nothing found" when in fact nothing ran.

$ErrorActionPreference = 'Stop'

$bash = Get-Command bash -ErrorAction SilentlyContinue
if (-not $bash) {
    $candidates = @(
        "$env:ProgramFiles\Git\usr\bin\bash.exe",
        "${env:ProgramFiles(x86)}\Git\usr\bin\bash.exe",
        "$env:LOCALAPPDATA\Programs\Git\usr\bin\bash.exe"
    )
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $found) { Write-Error 'bash not found. Install Git for Windows.'; exit 1 }
    $bashPath = $found
}
else { $bashPath = $bash.Source }

Push-Location $PSScriptRoot/..
try {
    & $bashPath '-lc' 'exec scripts/git_precommit_check.sh'
    exit $LASTEXITCODE
}
finally { Pop-Location }
