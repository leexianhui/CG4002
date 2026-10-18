# Full-system setup guide

The whole duel: six sensor boards → two relays → AI + engine on the Ultra96 → two phones.
Commands are grouped by the machine they run on, the same way as the comms demo.
Every long-running command needs **its own terminal**.

```text
 dev laptop (this one)      relay laptop (Qian Hui's)          Ultra96                     phones
 build + flash boards       P1 relay ══BLE══ P1 hub board      ai/classifier_server.py :8888  Termux tunnel
                            P2 relay ══BLE══ P2 hub board      cg-engine          :8443   Unity app
                            SSH tunnel ──────────────────────▶ (both)
```

| Machine | Runs | Reaches the Ultra96 by |
|---|---|---|
| Dev laptop | `pio` builds and flashing | — |
| Relay laptop | SSH tunnel, P1 relay, P2 relay | `ssh -J ... -L 8888 -L 8443` |
| Ultra96 | AI classifier (8888), game engine (8443) | — |
| Phone ×2 | Termux tunnel + `socat`, Unity app | `ssh -J ... -L 8443` |

## Secrets and settings per machine

Nothing new is needed: these are the same token, PSK and certificate as the comms demo. Only the BLE
addresses are new. The boards no longer need any Wi-Fi hotspot: all their links are BLE.

| Where | What | Set in | New? |
|---|---|---|---|
| **Ultra96** `comms/.env.engine` | `CG_ENGINE_BEARER_TOKEN` | already there from the demo | no |
| **Ultra96** `comms/certs/` | `engine.crt` + `engine.key` (CN `cg4002-engine`; SAN 127.0.0.1/localhost; valid to Sep 2027) | already there | no |
| **Relay laptop** `comms/.env.relay` | `CG_RELAY_PSK_HEX` (one key, both players) and `CG_RELAY_ENGINE_LINK__BEARER_TOKEN` (= the engine token) | already there | no |
| **Relay laptop** `comms/certs/engine.crt` | copy of the Ultra96's `engine.crt` (public cert only, not the key) | already there | no |
| **Relay laptop** terminal | `CG_RELAY_TRANSPORT__BLE_ADDRESS`, one per relay terminal | `$env:` in step 3 | **yes**: scan for `CG4002-Hardware-P1/P2` |
| **Dev laptop** terminal | `CG_GATEWAY_PSK_HEX` = the same PSK as `.env.relay`, only when building the two hubs | `$env:` in step 1 | changed: was hard-coded in `comms/firmware/include/cg_config.h`; the hub build now reads it from this variable |
| **6 boards** | nothing: each hub finds its own torso and left wrist over BLE | none | no. The hotspot SSID/password in the sketches is used only by the hardware team's recording (UDP) builds |
| **Phones** | engine token, typed into the app | Unity app **Bearer token** field | no (same token) |
| **Phones** (Termux) | your NUS SSH login for `stujump` + `xilinx@makerslab-fpga-53` | typed at the ssh prompt | no |
| **Ultra96 / relay laptop** | NUS SSH login (`e1138443`) | typed at the ssh prompt | no |

Player ids need no setup. `relay.hardware.p1.yaml` / `p2.yaml` set them and override `.env.relay`'s
`CG_RELAY_PLAYER_ID`. Ports are fixed: 8888 (AI), 8443 (engine), 8080 (phone `socat`).

What changed since the comms demo:
- The boards are the **real hardware**, built from `hardware/gateway/` (was `firmware_demo/`).
- The relay uses `configs/relay.hardware.p1.yaml` / `p2.yaml`, not `relay.p1.yaml`.
- The classifier is the **real AI** (`ai/classifier_server.py`), not `cg-ultra96-sim`.
- The engine uses `configs/engine.integration.yaml`, which waits until both phones are ready.
- The `--assessment`, `--latency-profile`, `--assess-seed`, `--trigger-map` and `throughput` flags are gone.

---

## 0. One-time setup (each machine, after this push)

From the repository root on **each** of the dev laptop, relay laptop and Ultra96:

```sh
git lfs install
git pull --ff-only origin main
git lfs pull
```

> **The folders moved.** `firmware_demo/` is now `comms/firmware/`, `firmware_gateway/` is now
> `hardware/gateway/`, `cg4002-hw-main/` is now `hardware/` and `CG4002-Viz-main/` is now
> `visualizer/`. On the Ultra96 and the relay laptop the pull handles this. Re-point Unity Hub at
> `visualizer/` (step 5).

Then update the comms package, from `comms/`:

```powershell
# relay laptop (Windows)
.\.venv\Scripts\python.exe -m pip install -e .
```

```sh
# Ultra96
.venv/bin/python -m pip install -e .
python3 -c "import numpy; print(numpy.__version__)"   # the AI needs numpy (PYNQ images have it)
```

Keep each machine's existing `comms/.env.relay`, `comms/.env.engine` and `comms/certs/`. They are
not in git, and they still work as before.

---

## 1. Dev laptop: build and flash the six boards

Each player has **three** boards, all BLE. The right wrist (hub) talks to the relay and is also
connected to its own torso and left wrist. No board uses Wi-Fi, so no hotspot is needed.

| Board | P1 env | P2 env | Needs the PSK? |
|---|---|---|---|
| Right wrist (FireBeetle, BLE hub) | `p1_hub` | `p2_hub` | yes: the comms PSK (`CG_RELAY_PSK_HEX` in `comms/.env.relay`; one key for both players) |
| Torso | `p1_torso` | `p2_torso` | no |
| Left wrist | `p1_left` | `p2_left` | no |

```powershell
cd C:\Users\T0332075\Desktop\CG4002
& "pio-venv\Scripts\pio.exe" device list           # plug boards in one at a time to learn each COM port
cd hardware\gateway
```

**Player 1** (one PowerShell window):

```powershell
$env:CG_GATEWAY_PSK_HEX = "<the 32-hex PSK from comms/.env.relay>"   # once per PowerShell window
& "..\..\pio-venv\Scripts\pio.exe" run -e p1_hub   -t upload --upload-port COM8   # <- your P1 right-wrist port
& "..\..\pio-venv\Scripts\pio.exe" run -e p1_torso -t upload --upload-port COMx   # <- P1 torso port
& "..\..\pio-venv\Scripts\pio.exe" run -e p1_left  -t upload --upload-port COMy   # <- P1 left-wrist port
```

**Player 2**: same window (same PSK), with `p2_hub` / `p2_torso` / `p2_left` and the P2 ports.

Reflash **all three** boards of a player together. Hub and nodes must agree on the uplink mode. By
default (event mode) the hub sends the relay one segment per swing its own segmenter accepts, and
the torso/left wrist do not stream samples to it. The old continuous 50 Hz stream is still there:
add `-DCG_GATEWAY_EVENT_MODE=0` to the `build_flags` of all three envs, and set
`segment_mode: false` in that player's relay YAML.

Watch a hub boot (close the monitor before flashing again):

```powershell
& "..\..\pio-venv\Scripts\pio.exe" device monitor -p COM8 -b 115200
```

Power the torso and left wrist in any order, before or after the hub. On the hub's serial monitor,
look for `[link] torso connected` and `[link] left_wrist connected`, then `all_nodes_ready`. A torso
or left wrist prints `[link] hub connected`. The sketches still print `initialized at IP 0.0.0.0`;
that is expected, no Wi-Fi is started.

The hardware team's recording tools (`listener_2p.py`) need their original Wi-Fi links: build all
three boards of that player with `-DCG_GATEWAY_NODE_LINK=0` added to their `build_flags` and start
their hotspot. See `hardware/gateway/README.md`.

---

## 2. Ultra96: start the AI and the engine

SSH in, using one terminal per service:

```sh
ssh -J e1138443@stujump.comp.nus.edu.sg xilinx@makerslab-fpga-53.ddns.comp.nus.edu.sg
```

**Terminal A: AI classifier** (from the repo root):

```sh
cd ai
python3 classifier_server.py --backend numpy     # or --backend fixed: the FPGA's exact maths on the ARM
```

Expect `classifier service listening on 127.0.0.1:8888  (backend=NumpyBackend)`.
Once that works end to end, try the FPGA (PYNQ normally needs root):

```sh
sudo -E python3 classifier_server.py --backend fpga --bit cnn.bit
```

> Run **either** the AI **or** `cg-ultra96-sim`, never both: they share port 8888. For canned,
> repeatable actions (e.g. a demo with no real swings), stop the AI and run from `comms/`:
> `.venv/bin/cg-ultra96-sim --host 127.0.0.1 --port 8888 --scenario configs/scenarios/demo_scenario.yaml`

**Terminal B: game engine** (from `comms/`):

```sh
.venv/bin/cg-engine --config configs/engine.integration.yaml
```

This engine waits in **WAITING** until both relays are connected, both weapons are selected and
both phones are calibrated and ready. That's expected.

---

## 3. Relay laptop: tunnel and two relays

**Terminal 1: SSH tunnel** (keep it open):

```powershell
ssh -J e1138443@stujump.comp.nus.edu.sg -L 127.0.0.1:8888:127.0.0.1:8888 -L 127.0.0.1:8443:127.0.0.1:8443 xilinx@makerslab-fpga-53.ddns.comp.nus.edu.sg -N
```

**Find the hub boards** (from `comms/`, with both hubs powered):

```powershell
.\.venv\Scripts\python.exe scripts\ble_scan.py
```

Note the addresses of `CG4002-Hardware-P1` and `CG4002-Hardware-P2`. They are new names; the old
demo boards were `CG4002-Demo-P1/P2`.

**Terminal 2: P1 relay** (from `comms/`):

```powershell
$env:CG_RELAY_TRANSPORT__BLE_ADDRESS = "AA:BB:CC:DD:EE:01"   # <- CG4002-Hardware-P1 from the scan
.\.venv\Scripts\python.exe -m cg_comms.relay.app --config configs\relay.hardware.p1.yaml --no-tunnel --console-level INFO
```

**Terminal 3: P2 relay** (from `comms/`):

```powershell
$env:CG_RELAY_TRANSPORT__BLE_ADDRESS = "AA:BB:CC:DD:EE:02"   # <- CG4002-Hardware-P2 from the scan
.\.venv\Scripts\python.exe -m cg_comms.relay.app --config configs\relay.hardware.p2.yaml --no-tunnel --console-level INFO
```

The PSK and engine token come from `.env.relay`, exactly as in the demo, and the player id comes
from the YAML. Only the BLE address differs per terminal. `$env:` values last only for that
terminal, which is what lets two relays share one checkout.

What healthy looks like in each relay:
1. `session_streaming`, then `weapon_changed` once that player holds the weapon-selection pose
   (the hub locks the weapon until it reboots).
2. Nothing while standing still. Each swing the hub accepts gives exactly one
   `segment_received` → `classifier_request` → `segment_classified` (the AI's answer, with
   `passes_gate`), then `action_classified` if it cleared the confidence gate. On the hub's serial
   monitor the same swing shows `[Ultra96 send queued] ...` then `[segment] sent seg=...`.
3. `ble_mtu_too_small_for_segments` at connect means Windows refused the larger BLE packet size;
   every segment will then fail. Report it: the uplink has to be split smaller.

### Only P1 has hardware? Simulate P2

A P2 relay with no board connected never starts a session, so the engine waits forever. Run a
simulated P2 board instead, on the relay laptop from `comms/`, in two more terminals:

```powershell
# Terminal 4: simulated P2 board (same PSK as .env.relay; it streams SWORD frames 50 times a second)
.\.venv\Scripts\python.exe -m cg_comms.mock.firebeetle_sim --psk-hex <PSK from .env.relay> --player-id 2 --tcp-port 9002 --scenario configs\scenarios\demo_firebeetle_scenario.yaml --loop-scenario
```

```powershell
# Terminal 5: P2 relay talking to the simulated board instead of BLE
.\.venv\Scripts\python.exe -m cg_comms.relay.app --config configs\relay.sim.p2.yaml --no-tunnel --console-level INFO
```

The fake P2 sends motion that the AI will classify, usually as IDLE. Phone P2 is still needed,
because the engine also waits for both phones to be ready.

---

## 4. Phones: tunnel to the engine

On **each** Android phone, in two Termux sessions (unchanged from the demo):

```sh
ssh -J e1138443@stujump.comp.nus.edu.sg -L 127.0.0.1:8443:127.0.0.1:8443 xilinx@makerslab-fpga-53.ddns.comp.nus.edu.sg -N
```

```sh
socat TCP-LISTEN:8080,reuseaddr,fork OPENSSL:127.0.0.1:8443,verify=0
```

---

## 5. Unity: build the visualizer onto both phones (dev laptop)

1. **Unity Hub → Add → Add project from disk → `CG4002\visualizer`**. Remove the old
   `CG4002-Viz-main` entry, because that folder no longer exists.
2. If Hub says the editor **6000.5.10f1** is missing: click **Install Editor version** (or use the
   [release page](https://unity.com/releases/editor/whats-new/6000.5.10f1)) and tick **Android Build
   Support**, **Android SDK & NDK Tools** and **OpenJDK**. Don't upgrade the project to a newer editor.
3. Open **`Assets/Scenes/OnlineScene.unity`**.
4. **File → Build Profiles → Android → Switch Platform.** Make sure `OnlineScene` is first in the scene list.
5. **Project Settings → XR Plug-in Management → Android:** ARCore ticked.
   **Player → Internet Access: Require.**
6. Phone on USB with developer mode and USB debugging on, then **Build And Run**. Repeat for the second phone.

In the app on each phone, pick **Player 1** or **Player 2** and tap **Connect**. That's all: the
endpoint defaults to `ws://127.0.0.1:8080` (the phone's `socat`), and the engine token is built in.
The phone remembers its player for next time.

The token comes from `visualizer/Assets/CG4002/Resources/engine_token.txt` (committed). Before every
build, and when the editor opens, Unity refreshes that file from `comms/.env.engine` (or `.env.relay`),
so after changing the engine token just rebuild. **CG4002 → Integration → Refresh Engine Token From
comms .env** does the same by hand. Both fields stay editable in the app if you ever need to override.

---

## 6. Calibrate the arena and play

1. Mark an **origin** on the floor and a straight **forward (+Z) line** from it with tape.
2. Each phone: tap the origin in AR, stand on the +Z line at least 0.3 m out, and press
   **Confirm shared +Z direction**.
3. Each player holds the weapon-selection pose until the hub buzzes twice (weapon locked).
4. **Show setup readings** on a phone: both relays connected, both ready, positions sensible. Two
   people 1 m apart along +Z should read about (0, 0) and (0, 1).
5. When all four conditions hold, the engine moves to **IN_PROGRESS**. Swing!

Reset the match (Ultra96, from `comms/`):

```sh
.venv/bin/python -m cg_comms.tools.reset_match --config configs/engine.integration.yaml --host localhost
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Relay never logs `weapon_changed`, no windows | The weapon is not locked on the hub yet (hold the selection pose), or the hub hasn't connected its torso/left wrist (no `[link] ... connected` on the hub serial: power them, and check they were flashed for the same player) |
| All three boards stay yellow | The hub has no node links. Check the hub serial for `[link]` lines; reflash all three boards of that player from the current repo (old node builds still wait for a Wi-Fi hotspot) |
| `window_dropped reason=classifier offline` | Terminal A (AI) is not running, or the SSH tunnel is down |
| `classifier correlation mismatch` | Something other than `ai/classifier_server.py` (or `cg-ultra96-sim`) is on port 8888, or an old copy of the AI server that does not echo `requestId`/`timestamp` |
| Always `IDLE` while swinging | Normal at low confidence: the gate is 0.6 (`min_confidence`). Check the AI terminal for replies, and see `ai/README.md` known gaps |
| `SPIN_ATTACK` / `UPPERCUT` swings do nothing | Expected: there are no game rules for them yet, so they map to `IDLE` |
| Never guards | No GUARD model was supplied, so guard is always `IDLE` |
| Engine stuck in WAITING or PAUSED | A phone isn't ready or calibrated, a relay is disconnected, or a position is more than 1 s old. Use **Show setup readings** |
| Relay can't reach the engine (TLS error) | `comms/certs/engine.crt` is missing on the relay laptop. Copy it from the Ultra96 as for the demo |
| Swings accepted on the hub (`action_accepted`) but no `segment_received` | Hub serial shows `[segment] undelivered`: the BLE link is losing chunks. `stream_decode_drop reason=bad_crc` on the relay: the BLE packet size is too small (see `ble_mtu_too_small_for_segments`). `segment_ignored`: the relay YAML lacks `segment_mode: true` |
| `segment_abandoned reason=timeout` | Some chunks of a swing never arrived within 2 s; that swing is skipped. Occasional is fine, constant means a weak link |
| Hub reboot-loops right after `[cfg] ... wifi_sleep=off` | Old hub build: re-flash `p1_hub`/`p2_hub` from the current repo (hub builds now keep Wi-Fi sleep on, which BLE requires) |
| Hub reboot-loops with `assert failed: vQueueDelete` after `[ble] free heap before init` | Out of heap at BLE init: a Wi-Fi build of the hub. Re-flash all three boards from the current repo (BLE node link, no Wi-Fi) |

---

## Development-only shortcuts (no hardware)

From `comms/` on any laptop:

```powershell
.\.venv\Scripts\python.exe -m pytest -q                   # 290 tests, incl. relay <-> real AI model
.\.venv\Scripts\python.exe -m cg_comms.tools.integration_mock --plain-local   # engine + 2 sim devices + sim AI
```

Check the AI service on its own (through the tunnel, or on the board): `python3 ai/client_test.py --n 50`.

To run the real AI locally instead of on the board (needs `pip install numpy` in that Python):

```powershell
python ai\classifier_server.py --backend numpy
```

More detail: [`INTEGRATION_RUNBOOK.md`](INTEGRATION_RUNBOOK.md) (phones and calibration),
[`../hardware/gateway/README.md`](../hardware/gateway/README.md) (boards),
[`../ai/README.md`](../ai/README.md) (AI contract and gaps).
