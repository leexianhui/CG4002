# cg_comms

Communications subsystem for CG4002: the relay process (FireBeetle <-> Ultra96 <-> Game Engine
bridge, one per player), the Game Engine (authoritative match state + WSS hub for relays and
visualizers), the binary wire protocol, and a mock layer standing in for hardware that isn't
available yet. See `docs/COMMS_IMPLEMENTATION_PLAN.md` for the full design; this file is just
"how do I run it."

## Layout

```
src/cg_comms/
  clock.py, logging_setup.py, config.py   # cross-cutting: time, structured logs, settings
  protocol/        # binary wire format: header, crypto, framing, CRC, payload tables (strict-typed)
  messages/        # pydantic app-layer JSON models + schema-version gate
  transport/        # ITransport + ble/tcp/loopback implementations
  relay/            # per-device session, windowing, reliability, heartbeat, Ultra96/engine clients
  engine/           # authoritative game state, resolver, reorder buffer, WSS server (strict-typed)
  mock/             # firebeetle_sim / ultra96_sim / visualizer_sim / scenario playback
  tools/            # latency_report.py (offline log summary)
configs/            # relay.p1.yaml, relay.p2.yaml, engine.yaml
scripts/            # gen_psk.py, gen_certs.sh, tunnel.sh
```

`shared/config/weapons.yaml` and `shared/config/match.yaml` (sibling `shared/` directory, not under
`comms/`) hold the game-balance data `engine/rules.py` loads — see `shared/README.md`.

## Setup

```bash
cd comms
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
cp .env.example .env   # then fill in CG_RELAY_PSK_HEX / bearer tokens — never commit .env
python scripts/gen_psk.py                              # prints a PSK hex; paste into .env
scripts/gen_certs.sh                                    # self-signed TLS cert for the engine's WSS listener
```

## Running (mock rig, no hardware)

```bash
# terminal 1: game engine
cg-engine --config configs/engine.yaml

# terminal 2: fake Ultra96
cg-ultra96-sim --port 8888 --scenario <path-to-scenario.yaml>

# terminal 3: fake FireBeetle for player 1 (relay connects to it over tcp)
cg-firebeetle-sim --psk-hex <hex> --player-id 1 --tcp-port 9001 --scenario <path-to-scenario.yaml>

# terminal 4: relay for player 1 (set transport.kind: tcp in relay.p1.yaml to point at the sim above)
cg-relay --config configs/relay.p1.yaml --no-tunnel

# terminal 5: fake visualizer
cg-visualizer-sim --url wss://127.0.0.1:8443/visualizer/1 --bearer-token <token> --player-id 1 \
  --scenario <path-to-scenario.yaml>
```

A scenario file is a flat YAML list under `steps:`, each `{delay_ms, kind, payload}` — see
`mock/scenario.py` for the loader and each sim's module docstring for the `kind`s it understands.

## Security

Never commit `.env`, `comms/certs/*.key`, or any PSK/bearer token — `scripts/gen_psk.py` generates
one and prints the hex; nothing in this repo stores a real key. The root `.gitignore` already
excludes `.env`, `*.key`, and `comms/certs/`.

## Out of scope for this pass

`tools/inspect.py` and `tools/replay.py` are deferred (not implemented). Fragment reassembly
(`MORE_FRAGMENTS`) is intentionally not implemented — a set fragmentation bit is a hard, logged
drop (`protocol/codec.py`, `DropReason.FRAGMENTED`). No `tests/` or golden vectors are included in
this pass; that's a separate effort.

## Pre-demo robustness behavior

BLE, classifier TCP, and engine WebSocket lifecycles now recover independently. Outage traffic is
bounded and not replayed into a restarted engine. Engine restart starts a fresh in-memory match;
an authenticated operator command can reset a match without restarting services.
See [the implementation report, contracts, and physical verification checklist](../docs/PRE_DEMO_ROBUSTNESS.md).
