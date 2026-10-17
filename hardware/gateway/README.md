# Real hardware gateway integration

Builds the **actual imported two-player sketches** in `hardware/2 players/`.
The right-wrist hub is the final board in that supplied topology. Internal links remain Wi-Fi/UDP;
only the hub uses BLE to the laptop. No additional board or internal BLE links are introduced.

```text
left wrist -- existing Wi-Fi/UDP + sample export --+
torso ------ existing Wi-Fi/UDP + sample export --+--> right-wrist hub -- BLE v1 --> relay
                                                     (own IMU too)
```

Most integration code lives here. The six teammate sketches have opt-in `CG_HARDWARE_GATEWAY`
hooks for initialization and sample export. Hubs also poll incoming samples and drive the existing
motor pin from authenticated engine feedback. Left wrists keep sampling after weapon lock but
still stop pose detection/broadcasts after lock. Original Arduino builds without the flag retain
their behavior. Stance detection, segmentation, templates, wiring and existing UDP messages stay
in the teammate sketches. Their `sendSegmentToUltra96` logging stub is not used as an AI result.

The shared transport lives in `comms/firmware/src/main.cpp` with explicit hardware I/O hooks;
the gateway build disables synthetic IMU data and buttons. The demo's default behavior is unchanged.
BLE handshake/retries run on a separate FreeRTOS task, with a locked sample mailbox and a bounded
feedback queue. Sensor reads, UDP and motor writes stay on the hardware main thread.

## Boundary

- Existing BLE v1 framing, encryption, UUIDs, sequences, reliable weapon/feedback and time sync.
  Firmware version 5; packet range 0x40–0x4F stays reserved.
- **Event mode (default, `CG_GATEWAY_EVENT_MODE=1`).** The hub sends nothing periodic but a 1 Hz
  heartbeat. Its own segmenter's `acceptSegment()` calls `sendSegmentToUltra96()`, which hands the
  accepted samples (the stroke plus its 25% padding, right wrist only) to the BLE task through
  `gatewaySegmentBegin/Sample/End`. The hub keeps them at 50 Hz (every other 100 Hz sample) in a
  3-slot queue (one sending, one waiting, one filling; when full, the oldest unsent segment is dropped
  and logged; one older than 1.5 s is discarded unsent). It sends each segment once as `SEGMENT_CHUNK`s
  (wire_protocol.md §3): 16 samples per 234-byte frame, up to 4 unACKed, each retried with identical
  ciphertext (150 ms, doubling, five sends). Segments longer than 4 s keep their centre. The relay
  ACKs each chunk, reassembles, and sends exactly one classifier request per segment
  (`relay/segments.py`: resampled to 50 Hz, centred, edge-padded or trimmed to 100 samples).
  Torso and left wrist do not export samples in this mode, so the hub's radio carries only the
  hardware team's own UDP traffic and short BLE bursts. The off-hand rows the classifier requires are
  a fixed at-rest placeholder, counted as stale, until a GUARD model needs them.
- **Continuous mode (`-DCG_GATEWAY_EVENT_MODE=0` on hub and nodes, `segment_mode: false` on the
  relay).** Right/left wrist and torso channels in 50 Hz `SENSOR_FRAME`s, windowed by stride on the
  relay. Axes preserve the original board orientation; accel uses 2048 counts/g, gyro 16.4
  counts/degree/second, with int16 saturation.
- Continuous mode only: internal UDP port 4215 (P1) / 4225 (P2) carries one 30-byte sample at 50 Hz
  from each non-hub node. Packed little-endian: magic u16=0x4743, version u8=1, player u8, role u8,
  reserved u8=0, boot u32, sequence u32, source_ms u32, six int16 axes. Roles: hub=0, torso=1,
  left=2. Existing diagnostic RAW streams and ports remain unchanged.
- Reject wrong identity/format, duplicate and reordered samples. A new boot can replace a stream
  after it has been stale for 100 ms. Samples older than 100 ms are omitted and wrist stale flags
  are set. This is arrival-age detection, not synchronized acquisition: peer clocks are retained
  internally but current BLE frames use the hub clock and latest received samples. Radio buffering
  and cross-board alignment must be measured before agreeing the real AI input contract.
- BLE v1 has no torso stale flag. Missing torso slots are zero; the current classifier consumes
  only the two wrists. Do not extend AI to torso using this layout without adding validity handling.
  Missing wrist slots are zero **and marked stale**, never silently treated as measurements.
- The real locked posture selection is sent using the existing weapon-event packet. UID is all
  zeros because this hardware does not supply RFID. Selection is announced again after reconnect.
- Engine feedback codes 0–3 drive hub vibration using supplied intensity, duration and repeat count
  (150 ms between repeats; accepted bounds: 0–100%, 1–2000 ms, 1–10 repeats). ACK means queued for
  application, not proof of physical motor movement. Duplicate packets do not queue twice; a full
  queue is not ACKed, permitting retry. Local hardware feedback remains and engine vibration takes
  priority while active. LED behavior stays with the original hardware logic.
- Battery millivolts are zero because no battery telemetry is supplied; they are not measurements.
- Internal UDP has the same trusted-network assumption as the existing hardware links. BLE
  authentication does not authenticate the origin of internal UDP samples.

## Build and use

From this directory, set `CG_GATEWAY_PSK_HEX` to the matching relay player's existing 32-hex-digit
key in the process environment, then run:

```powershell
..\..\pio-venv\Scripts\pio.exe run -e p1_hub -e p1_torso -e p1_left
..\..\pio-venv\Scripts\pio.exe run -e p2_hub -e p2_torso -e p2_left
..\..\pio-venv\Scripts\pio.exe test -e native_test  # needs host gcc/g++ on PATH
```

Use the matching player key for each hub build. Nodes need no relay key. Keys are written only
into ignored `.pio/build/<env>/private/gateway_secret.h`; firmware binaries also contain that key.
Without a key the hub build fails. Compile checks used an all-zero dummy key: **rebuild with the
real key before uploading**. No upload is part of these commands.

Profiles select FireBeetle ESP32 for hubs and ESP32 Dev Module for other nodes. Verify each physical
board, flash size and serial port before uploading. The hub requires the supplied 3 MB app partition
(`huge_app.csv`, no OTA slot) with at least 4 MB flash; it does not fit the usual small app partition.
The selected FireBeetle profile defaults to 16 MB flash, so match that setting to the physical board.
Existing hardware Wi-Fi settings remain in the teammate sketches.

Scan for `CG4002-Hardware-P1` / `CG4002-Hardware-P2`. In each relay process's `.env.relay`, set
`CG_RELAY_TRANSPORT__BLE_ADDRESS`, `CG_RELAY_PSK_HEX`, `CG_RELAY_ENGINE_LINK__BEARER_TOKEN`,
`CG_RELAY_ENGINE_LINK__HOST`, `CG_RELAY_ENGINE_LINK__CA_CERT_PATH` and `CG_RELAY_ULTRA96__HOST`
as appropriate. From `comms/`, run `cg-relay --config configs/relay.hardware.p1.yaml` (or P2).
These profiles use segment mode (`windowing.segment_mode: true`). The classifier must be started
separately: the relay always
connects to the configured classifier TCP host/port. `CG_RELAY_ULTRA96__USE_MOCK=true` only bypasses
SSH tunnel setup; it does not start or select a classifier implementation.

For a first deterministic, no-combat check, run from `comms/` in a separate terminal:

```powershell
.\.venv\Scripts\python.exe -m cg_comms.mock.ultra96_sim --host 127.0.0.1 --port 8888
```

Point the relay's `CG_RELAY_ULTRA96__HOST` / `CG_RELAY_ULTRA96__PORT` at that process. Its default
reply is IDLE; use an action-result `--scenario` for repeatable combat outcomes. This verifies the
connection and gameplay pipeline, not gesture recognition. The old button `--trigger-map` does
not apply to these hardware windows. Do not start `integration_mock` alongside
real relays: that launcher also creates simulated devices and relay sessions for both players.

## Physical acceptance still required

1. Build/upload the three matching profiles for one player, choose a weapon, verify the BLE
   handshake, that `ble_connected mtu_payload` is at least 234, and that each accepted swing on the
   hub gives exactly one `segment_received` and one `segment_classified` on the relay. In continuous
   mode instead verify 50 Hz reception and move each wrist separately to verify role/axis mapping.
2. Swing repeatedly as fast as possible: watch for `[segment] queue full` / `undelivered` on the hub
   and `segment_abandoned` on the relay. Repeat for hub/relay reconnect and weapon reannouncement.
3. Send engine feedback, retry the same packet, confirm one queued vibration; verify PWM/duration.
4. Repeat with both players together. Measure Wi-Fi/BLE coexistence, dropped frames, sampling rate,
   timing alignment and heap during a sustained run. Compile success does not establish these.
5. Complete phone calibration acceptance, then run with the real AI (`ai/classifier_server.py`).

Host checks on 17 October 2026 (event mode): 316 Python tests passed, all six gateway profiles and the
demo firmware cross-compile, and the chunk struct's field offsets were checked against the Python
layout with the ESP32 compiler. The new native segment-queue test (`test/test_segments`) is not yet
run (no host g++ here). Nothing has been flashed or tested on boards yet.

Host checks on 14 October 2026: 286 Python tests passed; both relay profiles load correctly
with environment-supplied addresses/credentials. Six native C++ boundary tests were added, but
could not execute on this machine because host gcc/g++ is absent. ESP32 cross-compilation
does not substitute for those native tests. No boards were flashed.
