// Demo-specific configuration — NOT part of the shared protocol. Fill in PSK_HEX from
// `comms/scripts/gen_psk.py`'s output before flashing, and keep it in sync with the value in
// comms/.env's CG_RELAY_PSK_HEX / relay.p1.yaml's psk field. Never commit a real PSK — this file
// ships with a placeholder and should stay that way in shared history (see firmware_demo/README.md).

#pragma once

// 16-byte PSK as a 32-hex-char string, e.g. output of comms/scripts/gen_psk.py. Same PSK for every
// player's board — the relay side reads one shared value too (comms/.env.relay's CG_RELAY_PSK_HEX),
// not a per-player key.
#define CG_DEMO_PSK_HEX "21989e2b4a2386720aeb84c13cfd1f89"

// Overridable via a PlatformIO build flag (`-D CG_DEMO_PLAYER_ID=2`) so a second player's env in
// platformio.ini (e.g. env:esp32dev_p2) can flash player-2 identity from the same source tree with
// no edits here — see that env's build_flags.
#ifndef CG_DEMO_PLAYER_ID
#define CG_DEMO_PLAYER_ID 1
#endif

// BLE identifiers advertised by this firmware. Paste these into relay.p<N>.yaml's
// ble_address/ble_notify_char_uuid/ble_write_char_uuid once the board is flashed and its MAC is
// known (ble_address is discovered by scanning, not set here). CG_DEMO_BLE_DEVICE_NAME is
// overridable via build flag for the same reason as CG_DEMO_PLAYER_ID above; the service/char UUIDs
// below are the fixed protocol values and are the same for every player's board.
#ifndef CG_DEMO_BLE_DEVICE_NAME
#define CG_DEMO_BLE_DEVICE_NAME "CG4002-Demo-P1"
#endif
#define CG_DEMO_BLE_SERVICE_UUID "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define CG_DEMO_BLE_NOTIFY_CHAR_UUID "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  // device -> laptop
#define CG_DEMO_BLE_WRITE_CHAR_UUID "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   // laptop -> device

// Push-button used as an RFID-reader stand-in (cycles SWORD -> SPEAR -> MACE on each press).
// Wire one leg to this GPIO and the other to GND; INPUT_PULLUP means a press reads LOW.
#define CG_DEMO_WEAPON_BUTTON_PIN 4

// Motion-trigger buttons (Category 1 — reach the engine via Ultra96 classification, never named
// directly by firmware). Trigger id 3 is the guard button; it is still a plain button here, guard
// state is a classifier output, not a firmware decision. Wire each leg to GND; INPUT_PULLUP.
#define CG_DEMO_ACTION_BUTTON_COUNT 3
#define CG_DEMO_ACTION_BUTTON_PINS { 25, 26, 27 }
#define CG_DEMO_ACTION_BUTTON_TRIGGER_IDS { 1, 2, 3 }
#define CG_DEMO_BUTTON_DEBOUNCE_MS 200

// Frames tagged CG_FLAG_MOTION_ACTIVE per capture. Must equal the relay's windowing.window_samples
// (comms/configs/relay.p1.yaml) — a mismatch means the relay's window never lines up with the
// firmware's capture and the trigger_id gets attached to the wrong (or no) SensorWindow.
#define CG_DEMO_CAPTURE_FRAMES 40

#define CG_DEMO_SENSOR_FRAME_INTERVAL_MS 20  // 50 Hz
#define CG_DEMO_HEARTBEAT_INTERVAL_MS 1000   // 1 Hz
