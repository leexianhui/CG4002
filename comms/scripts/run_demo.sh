#!/usr/bin/env bash
# Launches the whole demo rig, one process per terminal-visible log stream, and tears it all down
# on Ctrl-C. Written for the laptop-only bring-up described in comms/DEMO_RUNBOOK.md; the same
# processes go to the Ultra96 unchanged once that port happens.
#
# Why a script instead of five terminals: the start ORDER matters and the failure mode of getting it
# wrong is misleading rather than obvious. cg-relay resolves its Ultra96 and engine connections at
# startup (relay/app.py's run_relay calls ultra96.connect() and engine_link.connect() before it ever
# touches the device), so a relay started first dies with "[WinError 1225] The remote computer
# refused the network connection" and then retries against a backoff that reaches 10s -- long enough
# that a demo looks hung when in fact only the boot order was wrong.
#
# Usage:
#   scripts/run_demo.sh mock          # no hardware: cg-firebeetle-sim stands in for the ESP32
#   scripts/run_demo.sh ble           # real ESP32 over BLE (configs/relay.p1.yaml, transport.kind: ble)
#   scripts/run_demo.sh mock full     # same, but every log line on the console (debugging)
#
# Everything runs in the foreground of this one terminal, prefixed per component. To watch a single
# component instead, tail its JSONL file from comms/logs/ (see the runbook's "Reading the logs").

set -uo pipefail

MODE="${1:-mock}"
VERBOSITY="${2:-demo}"
cd "$(dirname "$0")/.."

# Windows-style path of this checkout, for matching against process command lines (which Windows
# reports with backslashes) in kill_stale below.
PWD_WIN="$(cygpath -w "$PWD" 2>/dev/null || pwd)"

if [[ ! -d .venv ]]; then
  echo "error: comms/.venv not found -- see comms/README.md Setup" >&2
  exit 1
fi
BIN=".venv/Scripts"                       # Git Bash on Windows; POSIX venvs use .venv/bin
[[ -d "$BIN" ]] || BIN=".venv/bin"

# The PSK and bearer token must match what the firmware was flashed with and what the engine expects.
# Read them from .env.relay/.env.engine rather than duplicating them here, so this script can never
# be the reason the two ends disagree.
PSK_HEX="$(grep -E '^CG_RELAY_PSK_HEX=' .env.relay | cut -d= -f2)"
BEARER="$(grep -E '^CG_ENGINE_BEARER_TOKEN=' .env.engine | cut -d= -f2)"
if [[ -z "$PSK_HEX" || -z "$BEARER" ]]; then
  echo "error: CG_RELAY_PSK_HEX (.env.relay) or CG_ENGINE_BEARER_TOKEN (.env.engine) is empty" >&2
  exit 1
fi

case "$MODE" in
  mock) RELAY_CONFIG="configs/relay.p1.mock.yaml" ;;
  ble)  RELAY_CONFIG="configs/relay.p1.yaml" ;;
  *)    echo "usage: $0 [mock|ble] [demo|full]" >&2; exit 1 ;;
esac

# Console filtering. At 50 Hz with stride 5 the rig emits ~10 windows/second, and each one produces a
# window_ready + ultra96_sim_classified + action_classified + action_not_resolved quartet -- roughly
# 40 console lines a second, of which the ~2 that matter (combat_resolved, and the visualizer seeing
# it) scroll past unread. That is unusable to demo from and unusable to debug from.
#
# So the default hides the per-window chatter and keeps the story: startup, timesync, every resolved
# hit, the HP changes the phones see, and anything at WARNING or above. `full` restores everything.
#
# This filters the CONSOLE only. The JSONL files in logs/ always contain every event at INFO, so
# nothing is lost -- see the runbook's "Reading the logs".
case "$VERBOSITY" in
  demo)
    # Events worth a demo screen. Anchored to the structlog console format's event-name column.
    KEEP='combat_resolved|visualizer_sim_match_state|match_over_actions_ignored|timesync_seeded'
    KEEP="$KEEP"'|_starting|ws_server_listening|ws_client_connected|firebeetle_sim_listening'
    KEEP="$KEEP"'|link_lost|link_degraded|heartbeat_stale|reconnect|weapon_confirmed|rfid'
    # Levels that must never be filtered, whatever the event name.
    KEEP="$KEEP"'|\[warning|\[error|\[critical|Traceback|Error|error:'
    # Lines this script prints itself (--- ... ---) must survive too.
    KEEP="$KEEP"'|^---|^\[[a-z0-9]+\] ---'
    ;;
  full) KEEP='' ;;
  *)    echo "usage: $0 [mock|ble] [demo|full]" >&2; exit 1 ;;
esac

# In BLE mode the relay dials one specific MAC. A stale ble_address fails as a bleak connect timeout
# tens of seconds in, which reads as "BLE is flaky" rather than "wrong address" -- so surface the
# address being used up front, where it can be compared against scripts/ble_scan.py's output.
if [[ "$MODE" == "ble" ]]; then
  BLE_ADDR="$(grep -E '^\s*ble_address:' "$RELAY_CONFIG" | head -1 | sed 's/.*ble_address:[[:space:]]*//; s/[[:space:]]*#.*//; s/"//g')"
  echo "--- ble_address: $BLE_ADDR (verify with: scripts/ble_scan.py) ---"
fi

# Kills every cg_comms process belonging to THIS checkout, by command line. Used both before starting
# (to clear leftovers from an earlier run) and on exit.
#
# Matching on the command line rather than on ports is essential: cg-relay is a pure TCP *client*, so
# it holds no listening socket and a port sweep cannot see it -- yet an orphaned relay is the most
# damaging leftover of all. It occupies the device sim's single accept slot, so the next run's relay
# is refused with "[WinError 1225] The remote computer refused the network connection" and retries on
# a backoff to 10s, which looks exactly like starting things in the wrong order.
#
# //T is required because the console entry points (.venv/Scripts/cg-*.exe) launch python.exe as a
# child: killing the wrapper alone leaves the actual Python process running and holding its socket.
# The filter is scoped to this directory so a second checkout, or an unrelated Python, is never hit.
kill_stale() {
  local label="$1"
  local pids
  # Matches the console-script wrappers (.venv/Scripts/cg-engine.exe, cg-relay.exe, ...) as they
  # appear on the command line. Matching 'cg_comms' instead would find nothing: the package name
  # never appears there, only the hyphenated entry-point exe names from pyproject.toml.
  pids="$(powershell.exe -NoProfile -Command "
    Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |
      Where-Object { \$_.CommandLine -like '*${PWD_WIN}*' -and \$_.CommandLine -like '*\Scripts\cg-*' } |
      ForEach-Object { \$_.ProcessId }" 2>/dev/null | tr -d '\r' | grep -E '^[0-9]+$')"
  [[ -z "$pids" ]] && return 0
  echo "--- $label: $(echo "$pids" | wc -l | tr -d ' ') stale cg_comms process(es) ---"
  for pid in $pids; do taskkill //PID "$pid" //F //T >/dev/null 2>&1; done
  sleep 1
}

PIDS=()
# The trap covers EXIT as well as INT/TERM, so a Ctrl-C runs cleanup once for the signal and again
# for the resulting exit -- and each `kill` of a still-shutting-down child can re-enter it. Latch it,
# or a single Ctrl-C prints "stopping 6 processes" several times and looks like a stuck teardown.
CLEANED=0
cleanup() {
  [[ "$CLEANED" == "1" ]] && return 0
  CLEANED=1
  echo
  echo "--- stopping ${#PIDS[@]} processes ---"
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
  wait 2>/dev/null
  kill_stale "cleanup"
}
trap cleanup EXIT INT TERM

# Clear leftovers from a previous run BEFORE starting, not only after. A run killed with anything
# other than a clean Ctrl-C (closing the terminal, a debugger detach, the OS reclaiming the console)
# never reaches the exit trap, and the resulting orphans make this run fail in ways that point at the
# code rather than at the previous run.
kill_stale "pre-flight"

# Prefixes each line with its component so one terminal stays readable. stdbuf keeps the output
# unbuffered -- without it Python batches stderr and the "live" demo output arrives in 4KB bursts,
# which looks like a stall.
#
# Output goes through a process substitution rather than a pipe (`cmd | sed &`) because in a pipeline
# `$!` is the PID of the LAST element -- sed -- not of the Python process. Killing that left every
# component running as an orphan still holding its port, so Ctrl-C appeared to work and the next run
# then failed to bind. With `> >(sed ...)` the backgrounded job is the command itself, so $! is the
# PID that actually needs killing (verified: the pipe form leaks, this form does not).
#
# grep runs before sed so the patterns match the component's own output, and --line-buffered keeps it
# live rather than batching into blocks (the same reason stdbuf is here).
run() {
  local name="$1"; shift
  if [[ -n "$KEEP" ]]; then
    stdbuf -oL -eL "$@" > >(grep --line-buffered -E "$KEEP" | sed -u "s/^/[$name] /") 2>&1 &
  else
    stdbuf -oL -eL "$@" > >(sed -u "s/^/[$name] /") 2>&1 &
  fi
  PIDS+=($!)
}

echo "--- mode: $MODE | relay config: $RELAY_CONFIG ---"

# 1. Engine first: it is the listener both the relay and the visualizers dial into.
run engine   "$BIN/cg-engine" --config configs/engine.yaml
sleep 3

# 2. Classifier stand-in, before the relay: see the header note on connect-at-startup.
run ultra96  "$BIN/cg-ultra96-sim" --port 8888 --scenario configs/scenarios/demo_scenario.yaml
sleep 2

# 3. Device. In ble mode the real ESP32 is already advertising, so nothing to launch.
if [[ "$MODE" == "mock" ]]; then
  run device "$BIN/cg-firebeetle-sim" --psk-hex "$PSK_HEX" --player-id 1 --tcp-port 9001 \
    --scenario configs/scenarios/demo_firebeetle_scenario.yaml --loop-scenario
  sleep 2
fi

# 4. Relay: connects outward to all three of the above.
run relay    "$BIN/cg-relay" --config "$RELAY_CONFIG" --no-tunnel
sleep 2

# 5. Both visualizers. Player 2's POSITION feed is not optional: the resolver refuses to adjudicate
# any attack while either player's position is missing or older than position_max_age_ms
# (shared/config/match.yaml, 1000ms), returning IGNORED. With only player 1 connected the entire
# chain runs perfectly and no combat ever resolves.
run viz1     "$BIN/cg-visualizer-sim" --url "wss://127.0.0.1:8443/visualizer/1" \
  --bearer-token "$BEARER" --player-id 1 \
  --scenario configs/scenarios/demo_visualizer_scenario.yaml --ca-cert certs/engine.crt
run viz2     "$BIN/cg-visualizer-sim" --url "wss://127.0.0.1:8443/visualizer/2" \
  --bearer-token "$BEARER" --player-id 2 \
  --scenario configs/scenarios/demo_visualizer_scenario.yaml --ca-cert certs/engine.crt

echo "--- all up. Ctrl-C to stop everything. ---"
wait
