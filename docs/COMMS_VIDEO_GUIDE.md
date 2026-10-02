# Communications video: recording and editing script

Prepared 2026-09-30 against `c6d9245`. The live procedure has been rehearsed successfully on the rig.
This document covers only the **video**. Protocol wording, the packet slide, both FSMs and the
concurrency diagram are already in [COMMS_LIVE_AND_VIDEO_GUIDE.md](COMMS_LIVE_AND_VIDEO_GUIDE.md)
(sections 7 and 8). Reuse them from there rather than rewriting them. Code line numbers below are
for `c6d9245`; re-check them if the source changes before you record.
The word-for-word narration for each chapter is in [COMMS_VIDEO_SCRIPT.md](COMMS_VIDEO_SCRIPT.md).

## 1. What the video must contain

| Rubric item | Tag | Covered by |
|---|---|---|
| Laptop <-> Ultra96: explain protocol + demo with dummy data | Live + Video | Ch. 3 slides + **Recording A** (relay + classifier + engine terminals) |
| Ultra96 <-> Visualizer: explain protocol + demo with dummy data | Live + Video | Ch. 4 slides + **Recording C** (phone) aligned with Recording A |
| FireBeetle setup | **Video only** | Ch. 1, **new clip S1** |
| Device IDs, packet types, packet format | Live + Video | Ch. 2 slides + **Recording B** (pio monitor) + **new clip S2** (`D` sample) |
| Concurrent connections from two FireBeetles | Video (and live) | Recording A: both relay consoles streaming at the same time |
| Maximize speed of transmission | Video (and live) | Ch. 6: rate comparison table from your saved JSON reports |
| Transfer a file | Video (and live) | **New clip S3** (`F`) |
| Reliability features | Video (and live) | Ch. 5 FSM + **new clip S4** (`L` then `F`) |
| Complete pipeline (keyboard -> board -> Ultra96 -> phone) | Live only, but the best evidence for the two network channels | Recordings A+B+C, one `b` transaction followed across all three |
| Encryption code walkthrough, **each** channel | **Video only** | Ch. 7 |
| Concurrency/threading walkthrough, laptop and Ultra96 | **Video only** | Ch. 8 |

Live-only items (1-minute stream with kbps, power reset, out of range) are **not required** in the
video. A short rate-table slide is still useful for "maximize speed" (Ch. 6).

### What your three recordings already cover

Each of the three recordings starts with process startup, runs through the connections and weapon
changes, and ends after `a`, `b`, `c` and `d`:

- **A: engine, classifier, relay P1 and relay P2 terminals.** Startup, `session_streaming` on both
  relays, `ws_client_connected` paths, `weapon_changed` -> `weapon_selected`, and for each key
  `assess_echo_sent` -> `assess_echo_verified` -> `assess_pipeline_forward` -> classifier request/response.
- **B: FireBeetle pio monitor.** `[boot] advertising`, `[ble] connected`, `[handshake] complete,
  session_id=...`, `[tx] sensor_frames=.../s`, `[assess] key=a trace=... weapon=...`, and
  `[assess] echo #n trace=... key=... kind=... transform=... in=(...) out=(...)`.
- **C: Unity phone.** Connection, weapon label, and each action's trace in the feed.

### Short clips still to record

Each clip takes one to two minutes with the rig already running. Use the same startup commands as in section 4 of the live guide.

| Clip | Content | Why |
|---|---|---|
| **S1** | Both boards, GPIO buttons and the two independent power sources. Relay laptop with no USB to the boards. The advertised names `CG4002-Demo-P1`/`CG4002-Demo-P2` in the relay scan or connect log | FireBeetle setup is video-only and has no other evidence |
| **S2** | Press uppercase `D` on P1, freeze on one decoded sample, press `D` off. Repeat on P2 | Human-readable packet (seq, player, raw/converted IMU, CRC, auth) |
| **S3** | `F`: 33 chunks, readback, CRC, SHA-256, then `Get-FileHash` on source and returned file | File transfer |
| **S4** | `L`, then `F`: `assess_fault_armed` -> `assess_fault_consumed` -> retransmit -> verified | Reliability features |
| **S5** (optional) | The throughput dashboard showing both players' Hz and kbps for about 10 s | Makes the rate slide concrete. It is not the live-only 1-minute test |

Screen-record code clips only if you can't use slides (see section 4). Slides stay readable after YouTube compression.

## 2. Aligning the three recordings

The recordings were started independently, so their timelines are offset. Align them on
**trace IDs**, not wall-clock time:

1. In Recording A, pause on the P1 relay's `assess_echo_sent` line for the `b` press and note its
   `trace=` value (for example `trace=7`).
2. In Recording B, find `[assess] echo #n trace=7 ...`. That is the board receiving and returning it.
3. In Recording A (Ultra96 terminal), find `ultra96_sim_classified ... demo_trace_id=7`, then the
   engine's `action_received` for the same trace.
4. In Recording C, find the phone feed line carrying the same trace.

Write down the four timestamps (A-relay, B, A-classifier/engine, C). They are your cut points for the
pipeline chapter. Do the same for one `a` press using `[assess] key=a trace=...` on the board and
`weapon_changed`/`weapon_selected` in A.

Also check the recordings before editing. Nothing below should be visible. Blur anything that is:
PSK hex, bearer tokens, `.yaml` files open in an editor, private key contents, passwords typed into SSH.

## 3. Chapter plan and narration

The suggested total is 12-15 minutes. Adjust it to the course's actual limit. Each chapter lists **on
screen**, **say** and **source**.

### Ch. 0: Title and topology (30 s)

- **On screen:** one slide. Two boards -> BLE -> one relay laptop running two relay processes ->
  SSH forward -> Ultra96 classifier `:8888` and engine `:8443` -> phone (Termux SSH + socat) -> Unity.
- **Say:** "Two independently powered boards, each with its own BLE session and relay process, on
  one laptop. The classifier and the game engine both run on Ultra96. The phone connects to the engine."

### Ch. 1: FireBeetle setup (1 min, video only)

- **On screen:** clip S1, then a slide of the `platformio.ini` environments.
- **Say:**
  - "P1 is the FireBeetle, built with `firebeetle32`. P2 is built with `esp32dev_p2`, which sets
    `CG_DEMO_PLAYER_ID=2` and the BLE name `CG4002-Demo-P2`."
  - "USB is only used for flashing. During the demo each board runs from its own power source."
  - "GPIO4 cycles the weapon. GPIO25/26/27 start a gesture capture."
- **Source:** `firmware_demo/platformio.ini:4,31-36`; `firmware_demo/src/main.cpp:202` (`BLEDevice::init`).
- Say which board model each one actually is. Don't call the P2 board a FireBeetle if it isn't one.

### Ch. 2: Device IDs, packet types and packet format (2 min, Live + Video)

- **On screen:**
  - The packet-layout slide from live guide section 7.2 (68-byte `SENSOR_FRAME`) and its type-code table.
  - Then clip S2: one decoded sample for each player.
  - Then Recording B's `[handshake] complete, session_id=...` and `[tx] sensor_frames=50/s`.
- **Say:**
  - "The BLE address identifies the device. The PLAYER_ID byte in the header identifies the
    player, and the relay rejects frames with the wrong player."
  - "The TYPE byte selects the payload layout."
  - "The body is our real IMU layout, not plain text."
- **Source:** `comms/src/cg_comms/protocol/constants.py:13-23,39-57`; `protocol/payloads.py:114-140`
  (the echo request/response carry the same sensor body plus `trace_id`, `keycode`, `payload_kind`).

### Ch. 3: Laptop <-> Ultra96 (2 min, Live + Video)

- **On screen:**
  - Slide: `SENSOR_WINDOW` JSON line -> `ACTION_RESULT` JSON line, newline-framed TCP.
    Show one real example taken from Recording A.
  - Code slide: the `readline` loop.
  - Then the Recording A excerpt for the aligned `b` trace: `classifier_request` -> `ultra96_sim_classified`
    -> `classifier_response` -> engine `action_received`.
- **Say:**
  - "TCP is a byte stream, so one send can arrive in several reads. Each record ends in a
    newline and both sides read with `readline`, which buffers until the newline arrives. A record
    without its newline at EOF is rejected."
  - "The classifier must echo `playerId`, `timestamp` and `demoTraceId` unchanged, otherwise the
    reply is rejected."
  - "Actions and weapon changes then go to the engine over a separate WebSocket."
- **Source:**
  - `relay/ultra96.py:60-86` (`_serve`: readline, unterminated-line check, futures by `request_id`)
    and `:156-167` (the correlation check).
  - `mock/ultra96_sim.py:165-193` (the server-side readline).
  - `protocol/framing.py:18-60`, which is the equivalent reassembly for the binary protocol on stream transports.
- For fragmentation, show the ultra96.py/ultra96_sim.py `readline` code, which is the TCP answer. Don't
  present the BLE framer as the TCP solution.

### Ch. 4: Ultra96 <-> Visualizer (1.5 min, Live + Video)

- **On screen:**
  - Slide: engine -> `/visualizer/<id>` WebSocket: `ACTION_RECOGNIZED`, `MATCH_STATE`, `COMBAT_EVENT`,
    weapon updates. Phone -> engine: `POSITION`.
  - Route: Unity -> `127.0.0.1:8080` socat -> TLS -> SSH forward -> engine `:8443`.
  - Then Recording C side by side with Recording A's engine lines for the same trace.
- **Say:**
  - "The engine owns the game state. It pushes recognised actions and state to each phone on the
    phone's own path, and the phone sends its position back."
  - "WebSocket framing handles message boundaries on this link."
- **Source:** `engine/ws_server.py:24-25` (paths), `:131-163` (handler); `messages/models.py:76,102,186`.

### Ch. 5: Complete pipeline + reliability (2.5 min)

1. **Pipeline (from A+B+C, cut on the timestamps from section 2).** Show one `a` press and one `b`
   press end to end: keyboard -> relay `assess_echo_sent` -> board `[assess] echo ... in=(...) out=(...)`
   -> relay `assess_echo_verified` -> classifier -> engine -> phone.
   Say: "The key selects dummy IMU data. The board returns it. The relay checks the returned values
   and only then sends the window to the classifier. The classifier here is a deterministic
   stand-in, not a trained model."
   Show `assess_dummy.py:36-40` for where B/C/D are defined, and `:71-104` for the fact that the file
   is re-read on every press, which is how the live edit works.
2. **Reliability FSM.** Use the reliable-operation state diagram from live guide section 8, Ch. 2. Explain:
   - Sensor frames are best effort. Control and assessment packets are ACKed and retried.
   - A retry is an identical frame, and the board answers it from its cached response, so a
     retried `a` does not cycle the weapon twice.
3. **Clips S3 and S4.**
   Say: "The board checks the CRC. The relay reads the bytes back from board RAM and compares
   SHA-256. Here I deliberately drop one ACK, and you can see the retransmission and the duplicate
   being ignored."
- **Source:**
  - `relay/reliability.py:52-72` (timeout 200 ms, x2 backoff, 5 attempts).
  - `firmware_demo/src/main.cpp:316-320` (response gates the ACK) and `:354` (echo print).
  - `protocol/codec.py:160-175` (`reliable_kind`).

### Ch. 6: Maximizing speed (1 min)

- **On screen:**
  - The 50/100/200 Hz rate table, filled from the saved JSON reports (the table is in the script, Ch. 6).
  - Optionally clip S5.
- **Say:**
  - "Only the firmware's frame interval changed: 20, 10 and 5 ms."
  - "kbps is application goodput: 42 payload bytes x 8 per received frame."
  - The results:
    - 50 Hz: both boards got the full rate with no gaps.
    - 100 Hz: P1 was starved, at 43.6 Hz.
    - 200 Hz: P2 was starved, at 130.1 Hz, while P1 hit 200 Hz.
  - Conclusion: "50 Hz is the highest rate where both boards stayed complete at the same time."
- **Source:** `firmware_demo/platformio.ini:60-79`; `firmware_demo/src/main.cpp:730-738` (absolute-deadline
  pacing); `firmware_demo/src/main.cpp:171-181` (connection-interval request comment).

### Ch. 7: Encryption on each channel (2.5 min, video only)

Use one slide per channel. Each slide has the channel diagram on the left and a 10-15 line code
extract on the right.

| Channel | Say | Code extract |
|---|---|---|
| Board <-> relay (BLE) | "AES-128-GCM with a pre-shared key. The 8-byte header is authenticated as AAD, the body is encrypted, and the 16-byte tag detects tampering. The nonce is derived from session, player, direction and extended sequence, and is never sent. CRC is checked first as a cheap corruption check; it is not security." | Python: `protocol/crypto.py:17-36` (`derive_nonce`, `seal`, `open_sealed`), `protocol/codec.py:150-157` (encode) and `:193-196` (CRC before decrypt). Firmware: `lib/cg_protocol/cg_crypto.cpp:28-60` (`mbedtls_gcm_crypt_and_tag` / `auth_decrypt`), `lib/cg_protocol/cg_frame.cpp:46-54` |
| Relay <-> classifier | "The JSON over TCP has no encryption of its own. On the network it runs inside our SSH local forward, which encrypts it." | `relay/ultra96.py:61` (connects to 127.0.0.1:8888) + the `ssh -J ... -L 8888 -L 8443` command |
| Relay <-> engine | "WebSocket over TLS. The relay verifies the engine certificate against our CA, and a bearer token authenticates the connection. The token is authentication, not encryption." | `relay/engine_link.py:50-57`; `engine/ws_server.py:29-36` |
| Engine <-> phone | "Unity talks plaintext only to socat on the phone's own loopback. socat opens TLS to the engine through SSH. socat runs with `verify=0`, so it does not check the certificate. The SSH host key is checked separately." | `EngineClient.cs` connect URL; the two Termux commands |

Keep PSK values, tokens and key files off every slide. Show the variable names, not their values.

### Ch. 8: Concurrency (2.5 min, video only)

- **On screen:** the concurrency block diagram from live guide section 8, Ch. 5. Walk the arrows, then give
  one short code slide per block:

| Block | Say | Code extract |
|---|---|---|
| Relay: two OS processes | "One relay process per player. Inside each, one asyncio event loop runs cooperative tasks. They overlap waiting on I/O; there is no thread per packet." | `relay/session.py:317-335` (stats, read, feedback, heartbeat, watchdog tasks) |
| BLE receive | "The bleak callback only queues bytes into a bounded queue. If the queue overflows, the relay reconnects explicitly instead of losing frames silently." | `transport/ble.py:47-55` |
| Classification | "Each window becomes its own task, so the stream doesn't wait for the classifier." | `relay/session.py:894` |
| Keyboard/file | "One assessment operation at a time, behind a lock, because the board caches only one response. Streaming carries on meanwhile." | `relay/session.py:121` (`_assess_send_lock`) |
| Engine link | "Separate receive and send tasks. The outbox is bounded, and nothing is replayed after a reconnect." | `relay/engine_link.py:61-79` |
| Ultra96 classifier | "`asyncio.start_server` runs one handler coroutine per connection, so P1 and P2 are served concurrently." | `mock/ultra96_sim.py:316-325` |
| Ultra96 engine | "The WebSocket server and the single game-state task run together. Handlers only enqueue events; only the game-state task changes HP. Each client has its own bounded queue and writer task." | `engine/app.py:127-130`; `engine/game_state_task.py:106-137`; `engine/ws_server.py:65-70,86-92` |
| Firmware | "The BLE write callback runs in the BLE stack's task and only puts the frame on a FreeRTOS queue. `loop()` drains it and paces sensor frames and heartbeats." | `firmware_demo/src/main.cpp:150-159`, `:422-425`, `:694-745` |
| Unity | "The receive loop runs off the main thread and pushes into a `ConcurrentQueue`. `Update()` drains it on the main thread, which is the only thread allowed to touch the UI." | `EngineClient.cs:49,169-191` |

- **Say the dependencies explicitly:**
  - No keyboard IMU is forwarded before the board's reply is verified.
  - No ACTION is sent before a matching classifier reply.
  - Weapon, position, guard and cooldown affect how the engine resolves an action.

### Ch. 9: Close (20 s)

- **On screen:** one slide mapping rubric items to the chapters in this video.

## 4. Production notes

- **Code on slides, not IDE screen recordings.** Use a monospace font at 20 pt or larger, 10-15 lines per
  slide, and highlight the lines you are talking about. Keep the original comments, since the rubric asks for commented code.
- **Terminal clips.** Crop and zoom to one terminal per shot in the editor. For the pipeline, a 2x2
  layout (relay | board / Ultra96 | phone) works only if each quadrant is readable at 1080p. If it isn't,
  show the steps one after another.
- **Colours.** Point out that P1 and P2 log lines have different colours (`logging_setup.py:36`).
  This meets the "different colours per connection" guideline.
- **Export** at 1080p or higher. Upload as unlisted and check readability in the YouTube player
  before submitting.
- **Stay honest on camera:**
  - The classifier is a deterministic simulator.
  - kbps is application goodput, not the BLE PHY rate.
  - The phone bridge uses `verify=0`.
  - Rates quoted must come from saved reports.

## 5. Checklist before upload

- [ ] S1-S4 recorded (S5 optional)
- [ ] One `a` and one `b` transaction aligned across A/B/C by trace ID
- [ ] Rate table filled from `logs\assessment\rate*-trial*.json`, not from the arithmetic targets
- [ ] Encryption: four channels, each with a code slide
- [ ] Concurrency: diagram plus laptop, Ultra96, firmware and Unity code slides
- [ ] FSM slides used for the connection and reliable-operation protocols
- [ ] No PSK, token, password or private-key contents visible in any frame
- [ ] Watched once at YouTube playback quality: every code slide readable
