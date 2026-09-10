// Demo-specific configuration — NOT part of the shared protocol. Fill in PSK_HEX from
// `comms/scripts/gen_psk.py`'s output before flashing, and keep it in sync with the value in
// comms/.env's CG_RELAY_PSK_HEX / relay.p1.yaml's psk field. Never commit a real PSK — this file
// ships with a placeholder and should stay that way in shared history (see firmware_demo/README.md).

#pragma once

// 16-byte PSK as a 32-hex-char string, e.g. output of comms/scripts/gen_psk.py.
#define CG_DEMO_PSK_HEX "21989e2b4a2386720aeb84c13cfd1f89"

#define CG_DEMO_PLAYER_ID 1

// BLE identifiers advertised by this firmware. Paste these into relay.p1.yaml's
// ble_address/ble_notify_char_uuid/ble_write_char_uuid once the board is flashed and its MAC is
// known (ble_address is discovered by scanning, not set here).
#define CG_DEMO_BLE_DEVICE_NAME "CG4002-Demo-P1"
#define CG_DEMO_BLE_SERVICE_UUID "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define CG_DEMO_BLE_NOTIFY_CHAR_UUID "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  // device -> laptop
#define CG_DEMO_BLE_WRITE_CHAR_UUID "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   // laptop -> device

// Push-button used as an RFID-reader stand-in (cycles SWORD -> SPEAR -> MACE on each press).
// Wire one leg to this GPIO and the other to GND; INPUT_PULLUP means a press reads LOW.
#define CG_DEMO_WEAPON_BUTTON_PIN 4

#define CG_DEMO_SENSOR_FRAME_INTERVAL_MS 20  // 50 Hz
#define CG_DEMO_HEARTBEAT_INTERVAL_MS 1000   // 1 Hz
