# PowerShell wrapper for scripts/git_bootstrap.sh. See that file for what it does and why.
#
#   .\scripts\git_bootstrap.ps1 "Xian Hui Lee" "you@personal.com" your-github-username
#
# WHY THIS EXISTS: typing `scripts/git_bootstrap.sh ...` at a PowerShell prompt does nothing at all --
# no output, no error, no exit code. PowerShell has no handler for .sh, so it hands the file to the
# Windows shell association and returns as if it succeeded. The setup silently does not happen, and
# the next command fails for a reason that has nothing to do with the real cause. Same reason
# run_demo.ps1 exists.

param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Email,
    [Parameter(Mandatory = $true)][string]$GitHubUser,
    [string]$RepoName = 'CG4002'
)

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
    # Arguments go through the ENVIRONMENT, not the command line, because PowerShell 5.1 mangles
    # quoted arguments containing spaces when it invokes a native executable: passing
    # "Xian Hui Lee" as one argument arrives as three, so user.name becomes "Xian" and user.email
    # becomes "Hui". A name with a space is the normal case here, so the command-line route is not
    # usable. Environment variables have no such quoting layer.
    $env:CG_GIT_NAME = $Name
    $env:CG_GIT_EMAIL = $Email
    $env:CG_GIT_USER = $GitHubUser
    $env:CG_GIT_REPO = $RepoName

    # Nothing is passed on the command line -- the script reads CG_GIT_* from the environment itself.
    # This is deliberate: bash.exe's command line gets re-parsed by the MSYS runtime, which strips the
    # double quotes, so even `"$CG_GIT_NAME"` written here arrives unquoted and word-splits.
    #
    # -l (login shell) so the MSYS profile builds a PATH with coreutils on it; without it `sed`,
    # `grep` and friends are "command not found" inside the script.
    & $bashPath '-lc' 'exec scripts/git_bootstrap.sh'
    exit $LASTEXITCODE
}
finally {
    Pop-Location
    Remove-Item Env:\CG_GIT_NAME, Env:\CG_GIT_EMAIL, Env:\CG_GIT_USER, Env:\CG_GIT_REPO -ErrorAction SilentlyContinue
}
