# CG4002 Comms demo firmware

FireBeetle ESP32 firmware for the 3-week Communications-subsystem demo. Speaks the real binary
wire protocol (`shared/protocol/wire_protocol.md`) to an unmodified `cg-relay` over real BLE, but
generates **synthetic** IMU/RFID data in place of real sensor hardware — see `docs/REPO_PLAN.md`
for the real wrist/belt firmware this is a stand-in for, and `comms/README.md` for the full
mock-rig this demo is layered on top of.

## Layout

- `include/cg_config.h` — demo-only settings: PSK, BLE UUIDs, button pin, send intervals.
- `lib/cg_protocol/` — protocol port: `cg_crypto.*` (AES-128-GCM via mbedTLS + nonce derivation),
  `cg_frame.*` (frame encode/decode), `cg_sequence.h` (replay window / outgoing counter).
  `cg_protocol.h` itself is `#include`d directly from `shared/protocol/` (build flag `-I../shared/protocol`)
  rather than copied, so this firmware and `comms/src/cg_comms/protocol/` can never drift apart.
- `src/main.cpp` — BLE GATT server, handshake, streaming loop.
- `test/` — native suites (`env:native_test`, no ESP32 needed), one directory per suite since each
  has its own `main()`:
  - `test_golden_vectors/` — CRC16 + nonce derivation against `shared/protocol/golden_vectors.json`,
    mirroring `comms/tests/test_golden_vectors.py`. Does not exercise AES-GCM itself natively
    (see file header).
  - `test_sequence_window/` — `CgSequenceTracker` replay-window behaviour, guarding the aliasing bug
    that silently killed the laptop→device direction ~57 s into every session (see file header).

## Build / flash

Requires [PlatformIO](https://platformio.org/) (`pip install platformio` or the VS Code extension).

```
cd comms/firmware
pio test -e native_test               # protocol parity checks, no hardware needed
pio run -e esp32dev -t upload --upload-port <COM_PORT>    # generic ESP32 DevKitC dev bench
pio run -e firebeetle32 -t upload --upload-port <COM_PORT>
pio device monitor -b 115200
```

`env:native_test` compiles for the **host**, so it needs a host toolchain (`gcc`/`g++`) on PATH —
the ESP32 cross-compiler alone is not enough. On a machine without one the suites cannot run;
`pio run -e esp32dev` still builds the firmware itself fine.

## Before flashing

1. Generate a PSK: `python comms/scripts/gen_psk.py`. Paste the hex into
   `include/cg_config.h`'s `CG_DEMO_PSK_HEX` **and** into `comms/.env`'s `CG_RELAY_PSK_HEX` (or
   `configs/relay.p1.yaml`'s `psk` field) — both ends must use the same key.
2. Wire the buttons. Every leg goes to GND; `INPUT_PULLUP` means a press reads LOW, so no external
   resistor is needed:

   | Button | GPIO (`cg_config.h`) | Wire protocol effect |
   |---|---|---|
   | Weapon | `CG_DEMO_WEAPON_BUTTON_PIN` = 4 | Cycles SWORD → SPEAR → MACE (RFID stand-in). Never named directly by firmware — the relay confirms it, the Ultra96 classifies against it. No windows are emitted until the first press |
   | Action 1 | `CG_DEMO_ACTION_BUTTON_PINS[0]` = 25, trigger id 1 | Tags a 40-frame capture with trigger id 1. What action this becomes is decided entirely by `demo_trigger_map.yaml` on the Ultra96 — the firmware never names an action |
   | Action 2 | `CG_DEMO_ACTION_BUTTON_PINS[1]` = 26, trigger id 2 | Same, trigger id 2 |
   | Action 3 (guard) | `CG_DEMO_ACTION_BUTTON_PINS[2]` = 27, trigger id 3 | Same, trigger id 3. This is still a plain button in firmware — guard is a classifier output (`action: IDLE` + `guard_state: GUARDING`), not a firmware decision |

   `CG_DEMO_ACTION_BUTTON_COUNT`, `_PINS`, and `_TRIGGER_IDS` are three parallel arrays in
   `cg_config.h` — index `i` in one corresponds to index `i` in the others.

3. `CG_DEMO_CAPTURE_FRAMES` (`cg_config.h`, currently 40) **must equal**
   `configs/relay.p1.yaml`'s `windowing.window_samples`. Each action-button press tags exactly
   `CG_DEMO_CAPTURE_FRAMES` consecutive `SENSOR_FRAME`s with `CG_FLAG_MOTION_ACTIVE` and a trigger
   id; the relay's `PlayerWindower` (in `trigger_mode`) counts frames until it has that many tagged
   ones, then emits exactly one `SENSOR_WINDOW`. If the two numbers disagree, the window boundary
   the relay expects never lines up with what the firmware actually tagged — the capture either
   straddles two window-attempts or never fills one, and no window is ever emitted for a press.
   Change both together, never one alone.
4. `CG_DEMO_BUTTON_DEBOUNCE_MS` (currently 200) applies to **every** button, weapon included — one
   physical press must produce exactly one edge. Too short a value and a single press can register
   as two firmware events, which the relay sees as two separate tagged captures (§15 row 5 in
   `docs/DEMO_TRIGGERED_ACTION_PLAN.md` — "one press produces multiple actions"). Raise this value
   first if that happens; do not touch the relay's refractory logic to compensate for a wiring/debounce
   problem.

## After flashing

1. Power on the board; it advertises BLE as `CG4002-Demo-P1` and blinks nothing but logs over
   serial (`pio device monitor`).
2. Scan for its MAC address (any BLE scanner app, or `bleak`'s scan example) and set
   `configs/relay.p1.yaml`'s `transport.ble_address` to it. `ble_notify_char_uuid` /
   `ble_write_char_uuid` are already the fixed values in `cg_config.h` — copy them in too.
3. Run `cg-relay --config configs/relay.p1.yaml --no-tunnel` and watch for `timesync_seeded` in
   its logs — that confirms the handshake succeeded.

## Verification

Bench-test the handshake + streaming against a real `cg-relay` (BLE transport) *before* touching
the Unity visualizer side — confirms the protocol port is correct in isolation. Watch relay logs
for `stream_decode_drop` / `handshake_decode_drop` warnings, which mean something in the frame
encode/decode or crypto port has drifted from the Python reference.

## Pre-demo reliability update

Deploy the updated relay before flashing this firmware: RFID retries now resend identical encrypted
bytes and require the relay's authenticated duplicate re-ACK path. Wire layouts/PSK/GPIO/rates are
unchanged. RX writes use a bounded FreeRTOS queue; stack rejection keeps RFID pending.
`sensor_frames` means accepted by the BLE stack, not proven radio delivery; serial also reports
`generated`, `dropped`, cumulative `notify_dropped` and `rx_dropped`.
See [the audit and hardware checklist](../../docs/PRE_DEMO_ROBUSTNESS.md).
Native tests require host gcc/g++, and cover CRC/nonce/sequence/reliability only (not ESP32 mbedTLS).
