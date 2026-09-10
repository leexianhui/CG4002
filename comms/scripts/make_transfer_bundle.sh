#!/usr/bin/env bash
# Builds the archive that carries this repo to the laptop that can SSH into the Ultra96, and from
# there to the board itself. Run from anywhere; it locates the repo root itself.
#
#   comms/scripts/make_transfer_bundle.sh            -> ../cg4002-bundle-<date>.tar.gz
#   comms/scripts/make_transfer_bundle.sh /tmp/out   -> /tmp/out/cg4002-bundle-<date>.tar.gz
#
# WHY A SCRIPT AND NOT "zip up the folder":
#
# 1. Secrets. comms/.env, .env.relay, .env.engine and certs/engine.key all live inside the tree that
#    a naive "right-click > Send to > Compressed folder" would swallow whole. That archive then goes
#    through Telegram -- a third-party server that keeps a copy on its infrastructure regardless of
#    what you delete afterwards. The PSK is the AES-128 key for the whole device link and the bearer
#    token authenticates to the engine; leaking either through a chat app is not recoverable by
#    deleting the message. This script excludes them by name and then VERIFIES the built archive is
#    clean, so the safe path is the lazy path.
#
# 2. Size. comms/logs/ is ~19 MB of JSONL and comms/.mypy_cache/ ~11 MB, against ~1.5 MB of actual
#    source. Both are regenerated on first run and neither means anything on the board.
#
# 3. Layout. cg_comms locates the repo root by walking up for a directory containing BOTH comms/ and
#    shared/ (config.py:15-21). shared/config/weapons.yaml and match.yaml are loaded at engine
#    startup, so an archive of comms/ alone dies with "could not locate repo root" -- a confusing
#    failure a long way from its cause. tar-ing from the repo root keeps them siblings.
#
# tar.gz, not .zip, on purpose: the PYNQ image has tar in the base system but not necessarily unzip.
# Telegram will carry either.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${1:-$REPO_ROOT/..}"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

STAMP="$(date +%Y%m%d)"
ARCHIVE="$OUT_DIR/cg4002-bundle-$STAMP.tar.gz"

# What goes in. Deliberately a short allow-list rather than "everything except": a new directory of
# generated artifacts or credentials appearing later should be excluded by default, not included by
# default. firmware_demo/ rides along because it is small and it is what proves the demo end to end
# on whichever laptop you end up flashing from.
INCLUDE=(
  comms/src
  comms/tests
  comms/configs
  comms/scripts
  comms/pyproject.toml
  comms/README.md
  comms/DEMO_RUNBOOK.md
  comms/.env.example      # the TEMPLATE, with placeholder values -- see note at the bottom
  shared
  docs
  firmware_demo/src
  firmware_demo/include
  firmware_demo/platformio.ini
  .gitignore
  .gitattributes
)

# Belt-and-braces. Everything here is already outside INCLUDE, but --exclude is what protects you the
# day someone adds `comms` (the whole directory) to the list above in a hurry.
EXCLUDE=(
  --exclude=.env
  --exclude=.env.relay
  --exclude=.env.engine
  --exclude='*.key'
  --exclude='*.pem'
  --exclude=certs
  --exclude=logs
  --exclude='*.jsonl'
  --exclude=.venv
  --exclude=__pycache__
  --exclude='*.pyc'
  --exclude=.mypy_cache
  --exclude=.ruff_cache
  --exclude=.pytest_cache
  --exclude=.hypothesis
  --exclude=.pio
  --exclude='*.egg-info'
)

cd "$REPO_ROOT"

MISSING=()
for path in "${INCLUDE[@]}"; do
  [[ -e "$path" ]] || MISSING+=("$path")
done
if ((${#MISSING[@]})); then
  printf 'error: expected path not found under %s:\n' "$REPO_ROOT" >&2
  printf '  %s\n' "${MISSING[@]}" >&2
  exit 1
fi

rm -f "$ARCHIVE"
tar -czf "$ARCHIVE" "${EXCLUDE[@]}" "${INCLUDE[@]}"

# --- Verify. An exclude list that silently stopped matching is worse than no exclude list, because
# --- you would still believe the archive is clean. So assert it against the real thing.
FORBIDDEN=$(tar -tzf "$ARCHIVE" | grep -E '(^|/)(\.env$|\.env\.relay|\.env\.engine|certs/)|\.key$|\.pem$' || true)
if [[ -n "$FORBIDDEN" ]]; then
  echo "ABORT: secrets found inside $ARCHIVE -- not shipping this. Offending entries:" >&2
  echo "$FORBIDDEN" >&2
  rm -f "$ARCHIVE"
  exit 1
fi

COUNT=$(tar -tzf "$ARCHIVE" | wc -l)
SIZE=$(du -h "$ARCHIVE" | cut -f1)
SHA=$(openssl dgst -sha256 "$ARCHIVE" | awk '{print $NF}')

cat <<EOF

Wrote $ARCHIVE
  $COUNT entries, $SIZE
  sha256 $SHA

Verified: no .env, no .env.relay, no .env.engine, no certs/, no *.key, no *.pem.

Check that sha256 on the receiving laptop before you extract -- Telegram re-encodes some
attachments, and a truncated tarball fails in ways that look like a code bug:
  openssl dgst -sha256 cg4002-bundle-$STAMP.tar.gz

Then, on the machine that can reach the board:
  tar -xzf cg4002-bundle-$STAMP.tar.gz -C ~/cg4002
  scp -J e1138443@stujump.comp.nus.edu.sg -r ~/cg4002 xilinx@makerslab-fpga-53.ddns.comp.nus.edu.sg:~/

The three things this archive deliberately does NOT contain, and how to recreate each on the board
(all of it takes about a minute -- see DEMO_RUNBOOK.md section 7):

  1. comms/certs/  -- regenerate ON THE BOARD, with its own IP in the SANs, which the old loopback
     cert does not have anyway:
         cd comms && scripts/gen_certs.sh ./certs 365 172.26.191.151
     Then copy only engine.crt back to each laptop as engine_link.ca_cert_path. engine.key never
     leaves the board.

  2. comms/.env.engine -- one line, CG_ENGINE_BEARER_TOKEN=<token>. Type it into the board over your
     SSH session; do not put it in a file that travels.

  3. comms/.env.relay -- stays on the relay laptop, which already has it. Nothing to do.

The bearer token must MATCH between .env.engine on the board and .env.relay on the laptop, and the
PSK must match .env.relay and the value burned into the FireBeetle firmware. Read them out of the
laptop's existing .env.relay rather than generating new ones, or the relay will authenticate against
an engine that disagrees with it and you will debug TLS for an hour.
EOF
