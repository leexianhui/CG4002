#!/usr/bin/env bash
# One-time git setup for this repo on THIS (work) laptop. Idempotent -- safe to re-run.
#
#   scripts/git_bootstrap.sh "Your Name" "you@personal-email.com" <github-username>
#
# e.g. scripts/git_bootstrap.sh "Xian Hui Lee" "xianhui@gmail.com" xianhui
#
# Everything here is deliberately `git config --local`, never `--global`. Four reasons this machine
# needs its own bootstrap rather than `git init` and go:
#
# 1. WRONG IDENTITY. The global identity on this laptop is the work one (Thales email). `git init`
#    inherits it, so the first commit is authored by your employer address and pushed to your
#    PERSONAL GitHub, where it is public and permanent in the commit objects -- rewriting history
#    later does not un-publish it. Local config wins over global, so setting it per-repo fixes this
#    without disturbing the work Bitbucket repos on this machine.
#
# 2. ZSCALER BREAKS TLS. Zscaler intercepts HTTPS with its own root CA. Git's bundled OpenSSL does
#    not trust it, so any push fails with "SSL certificate problem: unable to get local issuer
#    certificate". Windows DOES trust it (it is installed machine-wide), so pointing git at the
#    Windows cert store via http.sslBackend=schannel fixes it properly. Do NOT "fix" this with
#    http.sslVerify=false -- that disables certificate checking entirely on every fetch and push.
#
# 3. NO SSH. Port 22 is blocked outbound to every host on this laptop (Zscaler, not NUS -- verified
#    against github.com:22 as a control). So the remote must be HTTPS, and a personal access token is
#    the credential, not a key. See comms/scripts/check_ultra96_reach.ps1.
#
# 4. CREDENTIAL COLLISION. Two work Bitbucket credentials are already cached under this Windows user.
#    Git Credential Manager keys by host, so GitHub will not collide with Bitbucket -- but it will
#    happily reuse a wrong GitHub credential if one gets cached, so useHttpPath keeps it per-repo.

set -euo pipefail

# Arguments may arrive either positionally (Git Bash) or through the environment (the PowerShell
# wrapper). The env route exists because bash.exe launched from PowerShell has its command line
# re-parsed by the MSYS runtime, which strips the double quotes around "$1" -- so a name containing
# spaces, which is the normal case, arrives word-split as three separate arguments and user.name
# silently becomes just the first word. Environment variables are not subject to that re-parsing.
NAME="${1:-${CG_GIT_NAME:-}}"
EMAIL="${2:-${CG_GIT_EMAIL:-}}"
GH_USER="${3:-${CG_GIT_USER:-}}"
REPO_NAME="${4:-${CG_GIT_REPO:-CG4002}}"

if [[ -z "$NAME" || -z "$EMAIL" || -z "$GH_USER" ]]; then
  sed -n '2,10p' "${BASH_SOURCE[0]}" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ "$EMAIL" == *thalesgroup.com || "$EMAIL" == *gemalto.com ]]; then
  echo "refusing: '$EMAIL' is a work address, and this repo pushes to your personal GitHub." >&2
  echo "Commit authorship is permanent and public. Pass a personal email instead." >&2
  exit 1
fi

if [[ ! -d .git ]]; then
  # -b main: this machine's git is 2.44 and still defaults to 'master' unless told otherwise, while
  # GitHub creates repos expecting 'main'. Fixing the branch name now avoids a mismatched first push.
  git init -b main
  echo "initialized empty repo at $REPO_ROOT"
else
  echo "already a git repo; updating config only"
fi

# --- Identity: local ONLY. This is the whole point of the script.
git config --local user.name "$NAME"
git config --local user.email "$EMAIL"

# --- TLS via the Windows cert store, so Zscaler's root CA is trusted.
git config --local http.sslBackend schannel

# --- Credentials: bind the helper to the full URL path so a cached credential for one host/repo is
# --- never silently reused for another. Harmless if only one GitHub account is ever used here.
git config --local credential.useHttpPath true
git config --local credential.https://github.com.username "$GH_USER"

# --- Line endings. The .gitattributes already forces LF for source; this stops git from ALSO
# --- rewriting working-tree files on checkout, which is what produces "every file changed" diffs
# --- when the same repo is used from both Windows and the Ultra96's Linux.
git config --local core.autocrlf false

# --- Windows filesystem is case-insensitive; the Ultra96's is not. Without this, a file renamed only
# --- by case looks unchanged here and then breaks the import on the board.
git config --local core.ignorecase false

REMOTE_URL="https://github.com/$GH_USER/$REPO_NAME.git"
if git remote get-url origin >/dev/null 2>&1; then
  CURRENT="$(git remote get-url origin)"
  if [[ "$CURRENT" != "$REMOTE_URL" ]]; then
    echo "note: origin was $CURRENT, leaving it alone. To change it:"
    echo "  git remote set-url origin $REMOTE_URL"
  fi
else
  git remote add origin "$REMOTE_URL"
  echo "added origin -> $REMOTE_URL"
fi

echo
echo "--- local config (global config untouched) ---"
git config --local --list | grep -E '^(user|http|credential|core|remote)' | sed 's/^/  /'

cat <<EOF

Next:

  1. Create an EMPTY repo named '$REPO_NAME' on your personal GitHub, PRIVATE.
     Do not let it add a README or .gitignore -- that creates a commit you would have to merge.
     Private matters: this repo is coursework, and a public one invites plagiarism accusations
     in both directions.

  2. Make a Personal Access Token to use as your push password:
     GitHub > Settings > Developer settings > Personal access tokens > Fine-grained
     Repository access: only '$REPO_NAME'. Permission: Contents = Read and write. That is all it
     needs. Set an expiry past your demo date.

  3. Check what is about to be committed, BEFORE committing:

       scripts/git_precommit_check.sh

  4. Then:

       git add -A
       git commit -m "Comms subsystem: relay, engine, protocol, mocks, demo rig"
       git push -u origin main

     Git will prompt for username and password; the password is the TOKEN, not your GitHub password.
     Windows will cache it after the first success.
EOF
