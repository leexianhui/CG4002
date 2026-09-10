#!/usr/bin/env bash
# Stands up the SSH tunnel to the Ultra96 by hand, for testing outside relay/tunnel.py's own
# TunnelSupervisor (COMMS_IMPLEMENTATION_PLAN.md §8, §9 — same jump-host topology).
#
# Usage: comms/scripts/tunnel.sh <jump_user> <jump_host> <target_user> <target_host> [local_port] [remote_port]

set -euo pipefail

JUMP_USER="${1:?jump_user required}"
JUMP_HOST="${2:?jump_host required}"
TARGET_USER="${3:?target_user required}"
TARGET_HOST="${4:?target_host required}"
LOCAL_PORT="${5:-8888}"
REMOTE_PORT="${6:-8888}"

exec ssh -J "${JUMP_USER}@${JUMP_HOST}" \
  -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
  -N "${TARGET_USER}@${TARGET_HOST}"
