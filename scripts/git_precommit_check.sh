#!/usr/bin/env bash
# Audits what git is ABOUT to commit, before you commit it. Run from anywhere in the repo:
#
#   scripts/git_precommit_check.sh
#
# WHY: pushing a secret to GitHub is not undoable. Deleting the file in a later commit leaves the
# value in the object history; force-pushing over it leaves it reachable through the reflog and any
# fork or cached view GitHub already made. The only real remedy is rotating the key. Since this repo
# holds an AES-128 PSK (also burned into the FireBeetle firmware), an engine bearer token, and a TLS
# private key, the cheap move is to check first -- once -- rather than to trust .gitignore blindly.
#
# .gitignore is necessary but not sufficient: it only applies to files git does not already track. A
# file added before a rule existed stays tracked and keeps getting committed, silently. This checks
# the actual staged/untracked reality instead of re-reading the rules.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

FAIL=0

echo
echo "=== 1. Identity this commit would carry ==="
NAME="$(git config user.name || echo '(unset)')"
EMAIL="$(git config user.email || echo '(unset)')"
echo "  $NAME <$EMAIL>"
if [[ "$EMAIL" == *thalesgroup.com || "$EMAIL" == *gemalto.com ]]; then
  echo "  PROBLEM: that is a work address, on a repo that pushes to personal GitHub." >&2
  echo "  Fix: git config --local user.email you@personal.com" >&2
  FAIL=1
else
  echo "  ok (not a work address)"
fi

echo
echo "=== 2. Secrets in what git would include ==="
# Ask git itself what it would commit -- tracked plus untracked-but-not-ignored -- rather than
# scanning the filesystem, so the answer reflects git's view, which is the one that matters.
CANDIDATES="$(git status --porcelain=v1 --untracked-files=all 2>/dev/null | sed 's/^...//' || true)"
if [[ -n "$(git rev-parse --verify HEAD 2>/dev/null || true)" ]]; then
  CANDIDATES="$CANDIDATES
$(git ls-files)"
fi

SECRETS="$(printf '%s\n' "$CANDIDATES" | grep -vE '^$' | sort -u | grep -iE \
  '(^|/)\.env($|\.)|(^|/)\.env\.(relay|engine)$|\.key$|\.pem$|(^|/)certs/|(^|/)secrets/' \
  | grep -vE '\.env\.example$' || true)"

if [[ -n "$SECRETS" ]]; then
  echo "  STOP -- these would be committed:" >&2
  printf '    %s\n' $SECRETS >&2
  echo "  None of these belong on GitHub. If a file is TRACKED, .gitignore will not save you:" >&2
  echo "    git rm --cached <file>" >&2
  FAIL=1
else
  echo "  ok -- no .env, .env.relay, .env.engine, certs/, *.key or *.pem"
fi

echo
echo "=== 3. A secret pasted into an otherwise-normal file ==="
# The PSK and bearer token are both 32 hex chars. A rule that specific gives near-zero false
# positives, unlike a generic entropy heuristic -- and these are the two values that actually exist.
#
# Scans the same candidate set as check 2, NOT `git grep`: git grep only searches files git already
# tracks, so a key pasted into a brand-new file -- the likeliest way this happens -- would be invisible
# right up until the commit that publishes it.
# Recognize the standard test-vector placeholders so they don't have to be hand-waved past on every
# run: NIST-style sequential bytes (00 01 02 ... 0f, either direction), a single digit repeated 32x,
# and the "0123456789abcdef" pattern doubled. shared/protocol/golden_vectors.json uses the first of
# these on purpose -- a public, guessable key that only exercises the wire-protocol test vectors and
# is never the key anything real authenticates with.
is_placeholder_hex() {
  local h="${1,,}"
  [[ "$h" == "000102030405060708090a0b0c0d0e0f" ]] && return 0
  [[ "$h" == "0f0e0d0c0b0a09080706050403020100" ]] && return 0
  [[ "$h" == "0123456789abcdef0123456789abcdef" ]] && return 0
  # Repeated single char (aaaa...., 0000...): bash's [[ =~ ]] is POSIX ERE and does not support
  # backreferences, so \(.\)\1* cannot express this here -- collapse runs instead and check length 1.
  local collapsed
  collapsed="$(printf '%s' "$h" | tr -s "${h:0:1}")"
  [[ "${#collapsed}" -eq 1 ]] && return 0
  return 1
}

PATTERN='(psk_hex|PSK_HEX|bearer_token|BEARER_TOKEN)[^=:]*[=:][[:space:]]*.?[0-9a-fA-F]{32}'
HITS=''
while IFS= read -r f; do
  [[ -n "$f" && -f "$f" ]] || continue
  case "$f" in *.env.example) continue ;; esac
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    hex="$(grep -oE '[0-9a-fA-F]{32}' <<<"$line" | tail -1)"
    is_placeholder_hex "$hex" && continue
    HITS="$HITS$f:$line
"
  done < <(grep -nIE "$PATTERN" -- "$f" 2>/dev/null | head -3)
done <<< "$(printf '%s\n' "$CANDIDATES" | grep -vE '^$' | sort -u)"

if [[ -n "${HITS//[[:space:]]/}" ]]; then
  echo "  suspicious -- a real-looking key assigned in a file that would be committed:" >&2
  printf '%s\n' "$HITS" | grep -vE '^$' | sed 's/^/    /' >&2
  echo "  (placeholders like 000...0 or 'replace-me' are fine; a random 32-hex value is not)" >&2
  FAIL=1
else
  echo "  ok -- no 32-hex key literals"
fi

echo
echo "=== 4. Size (GitHub warns >50 MB/file and dislikes big repos) ==="
BIG="$(git status --porcelain=v1 --untracked-files=all 2>/dev/null | sed 's/^...//' \
  | while IFS= read -r f; do
      [[ -f "$f" ]] || continue
      sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
      (( sz > 5000000 )) && printf '%8.1f MB  %s\n' "$(echo "$sz/1048576" | bc -l)" "$f"
    done || true)"
if [[ -n "$BIG" ]]; then
  echo "  files over 5 MB:"; printf '%s\n' "$BIG" | sed 's/^/  /'
else
  echo "  ok -- nothing over 5 MB"
fi

TOTAL=$(git status --porcelain=v1 --untracked-files=all 2>/dev/null | wc -l)
echo "  $TOTAL paths would be added/changed"

echo
echo "=== 5. Build artifacts that should be ignored but are not ==="
LEAK="$(git status --porcelain=v1 --untracked-files=all 2>/dev/null | sed 's/^...//' \
  | grep -iE '(^|/)(\.venv|pio-venv|\.pio|node_modules|__pycache__|\.mypy_cache|\.ruff_cache|\.pytest_cache|logs)/' \
  | head -5 || true)"
if [[ -n "$LEAK" ]]; then
  echo "  these look like generated output; check .gitignore:" >&2
  printf '    %s\n' $LEAK >&2
  FAIL=1
else
  echo "  ok -- no venvs, caches, .pio or logs"
fi

echo
if (( FAIL )); then
  echo "RESULT: do NOT commit yet -- fix the items marked above." >&2
  exit 1
fi
echo "RESULT: clean. Safe to commit and push."
echo
