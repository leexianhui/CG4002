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
cd firmware_demo
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
2. Wire a push-button between `CG_DEMO_WEAPON_BUTTON_PIN` (GPIO4 default) and GND — it stands in
   for the RFID reader, cycling SWORD → SPEAR → MACE on each press.

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
