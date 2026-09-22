# Pre-demo robustness and correctness report

Implemented 2026-09-21. This report describes the current code, not a claim that the physical rig has
been exercised. Classification remains on Ultra96, with separate P1/P2 relay processes and the same
8888 classifier / 8443 engine / phone SSH + socat arrangement.

The 2026-09-22 [focused follow-up](GUARD_LATENCY_FOLLOWUP.md) records the subsequent hardware-log
findings, controlled avatar rotations, classifier latency probe and richer drop diagnostics.

## A. Files changed

Paths are relative to the repository. Some files serve more than one category.

| Category | Files |
|---|---|
| Diagnostics | `comms/src/cg_comms/relay/windowing.py`, `relay/session.py`, `engine/game_state_task.py`; `firmware_demo/src/main.cpp` |
| Connection/recovery | `comms/src/cg_comms/relay/{app,ultra96,engine_link,reconnect,tunnel}.py`, `transport/ble.py`, `engine/ws_server.py`, `mock/ultra96_sim.py`; `comms/tests/test_pre_demo_recovery.py` |
| Firmware/BLE/protocol | `firmware_demo/src/main.cpp`, `lib/cg_protocol/{cg_sequence.h,cg_reliable.h,cg_crypto.cpp}`, `platformio.ini`, `test/test_sequence_window/test_sequence_window.cpp`, `test/test_reliable/test_reliable.cpp`; `comms/src/cg_comms/protocol/{codec,crypto,constants}.py`, `relay/{session,reliability}.py`, `mock/firebeetle_sim.py`; `comms/tests/test_pre_demo_protocol.py` |
| Combat ordering | `comms/src/cg_comms/engine/{game_state_task,inbound,reorder,resolver,app}.py`, `config.py`, `shared/config/match.yaml`; `comms/configs/engine.yaml` (source-of-truth comment only); `comms/tests/{test_pre_demo_combat,test_game_state_task_clock_skew,test_resolver,test_soak}.py` |
| Position contract/tests | `comms/src/cg_comms/engine/coordinates.py`, `comms/tests/test_pre_demo_combat.py`, `shared/schemas/position.schema.json`, `shared/protocol/app_messages.md`; active Unity `EngineClient.cs` and `DemoHudController.cs` |
| Unity/reset | Active project `visualizer_demo/CG4002VisualizerDemo_UnityTemp/My project/Assets/Scripts/Net/EngineClient.cs`, `Assets/Scripts/Controllers/DemoHudController.cs`, `Assets/Editor/CreateDemoScene.cs`; `comms/src/cg_comms/tools/reset_match.py`, engine routing/state/server files above |
| Documentation | This report, `comms/README.md`, `comms/DEMO_RUNBOOK.md`, `firmware_demo/README.md`, `visualizer_demo/README.md`, `shared/protocol/{wire_protocol,app_messages}.md` |

The pre-existing scene/render-settings changes and untracked personal files were not edited by this
work. `log.txt`, PSK, bearer token, BLE addresses, serialized scene connection values, phone tunnels,
TLS/socat setup, and the older Unity scaffold were left alone.

## B. Runtime behavior changes

- Suppression reports `no_weapon`, `too_few`, `stale`, or `span`, with player, trigger, capture ID,
  current/captured weapon, actual/expected frames, actual/expected span, deviation, and stale percent.
  Capture starts, ready windows, classifier requests/results, source timestamps, normalized engine
  timestamps, and combat outcomes can be correlated without adding fields to ACTION.
- Triggered captures use only frames with the original trigger tag. An early untagged/different-tag
  frame aborts an incomplete capture. A lost frame can therefore produce `too_few` where the old
  code silently filled with unrelated frames. The 40-frame, 780 ms nominal span, 15% deviation and
  20% stale thresholds are unchanged.
- The capture's weapon is fixed when triggered capture begins (continuous mode: at window creation).
  Classification latency cannot change that weapon. ACTION still represents the window-end time;
  it is not a new estimate of physical motion onset.
- Silence really expires handshake deadlines: 30 seconds for initial HELLO, 5 seconds per relay
  time-sync request wait. Firmware waits 5 seconds for HELLO_ACK and 2 seconds for each time-sync
  response, then retries a fresh handshake; a received restart HELLO makes the relay reconnect.
  Coalesced TCP frames survive transitions between handshake receives.
- BLE receive processing no longer waits for classifier replies or feedback ACK retries. Classified
  work is capped at configured `max_in_flight` (2 in demo YAML); no waiting-request backlog exists.
  Capacity/offline windows are explicitly dropped. Pending classifier work is cancelled on BLE loss.
- BLE receive queue: 256 notifications, overflow logged and session closed rather than silently
  corrupting a partial byte stream. Writes are serialized and bounded by the existing 3-second timeout.
  Disconnection wakes readers. Negotiated payload MTU is inspected after subscription; less than
  68 bytes cannot carry this unfragmented SENSOR_FRAME and fails explicitly.
- Firmware receives copied BLE writes through a 16-entry FreeRTOS queue instead of an overwritable
  single slot. RFID stays pending even if its first notification is rejected by the stack. On fresh
  handshake it clears capture/retry/replay state and reannounces the retained weapon. Rapid BLE
  disconnect/reconnect cannot mark an old handshake complete on the new connection.
- Engine inbox (512), reorder storage (2048), relay engine outbox (32), BLE feedback queue (8),
  reliable duplicate caches (16 each), and Unity inbox (256, 64 KiB message limit) are bounded.
  Existing engine per-client send queues remain bounded and drop oldest on overflow.
- Engine restart is a fresh in-memory match. Reset clears HP/phase/winner/cooldowns/weapons/guards/
  positions and buffered combat, preserves registrations and established clock offsets, rejects
  known pre-reset timestamps, and broadcasts the fresh state.
- Unity has one owned reconnect loop, captured socket references, finite connection/send timeouts,
  serialized position sends, and pause/resume teardown. No asynchronous old close can dispose its
  replacement socket. The generator refuses to overwrite an existing Demo.unity.

## C. Reconnection state machine

Classifier and engine each have exactly one worker: CONNECTING -> CONNECTED -> DISCONNECTED ->
BACKOFF -> CONNECTING. Retry delays are 1/2/4/8 seconds, capped at 8; after a connection lasts at least
10 seconds the failure count resets. Connection attempts are bounded at 5 seconds. Shutdown cancels
and awaits owned tasks. Startup proceeds even if either upstream is unavailable.

| Failure/restart | Recovery and preserved state |
|---|---|
| P1 or P2 board | Only that BLE session ends. Existing configured BLE backoff (1/2/4/8/10 s) applies. New Session gets fresh sequence/replay/time-sync state; partial windows and pending classification/feedback are discarded. Upstream sockets and the other player's relay continue. Held weapon is reannounced after reconnect; rebooted board starts with NONE and needs GPIO4 again. |
| P1 or P2 relay | Its sockets close; the other relay continues. New process starts independent upstream workers and fresh BLE handshake. Engine reanchors the replaced relay channel and rebuilds presence. Already normalized events and gameplay state remain; a later reconnect cannot change the guard used for an earlier queued attack. |
| Classifier | EOF/socket errors trigger retry; pending requests fail promptly. A silent tunnel is detected on the next request's configured timeout (120 ms in demo YAML), which closes that TCP connection. New windows work after reconnect. BLE and engine stay up. No classifier heartbeat was added, so an idle blackhole is discovered on the next request. |
| Engine | WS close/errors or 5-second ping/5-second ping-timeout detect loss. Relays reconnect on their same /relay/1 or /relay/2 paths. Engine starts with fresh match state. Phones reattach and get MATCH_STATE; future ACTION carries its weapon even if no new RFID press occurred. |
| Unity | Close/restart reconnects on its own /visualizer/id. Retry 1/2/4/8 s; connection timeout 5 s; position-send timeout 3 s aborts a stuck socket. Foregrounding after pause reconnects. Other services continue. |
| SSH tunnel | The links actually using it fail/retry independently. Restoring the existing forwarding restores those links without restarting BLE. If the optional relay-owned classifier SSH supervisor is enabled, it starts without gating BLE and is cancelled cleanly at shutdown. Manual phone SSH/socat management remains unchanged. |

Outage actions are not replayed. An action accepted by a TCP socket immediately before failure has no
application-level delivery acknowledgement and may be lost. No exactly-once engine-action guarantee
or match persistence has been invented. Identity remains the configured player/path throughout.

## D. Firmware / BLE / protocol reliability audit

All generated packets take a fresh per-direction extended sequence; wire SEQ is its low 16 bits.
GCM nonce includes session (HELLO uses device nonce), player, direction and extended sequence.
CRC is checked before decryption; authenticated identity is enforced per relay. Replay state is
committed only after successful authentication. Both sides reset session sequence/replay/retry state
for a fresh protocol session.

| Packet | Sender / receiver | ACK and retry / duplicate and failure behavior |
|---|---|---|
| SENSOR_FRAME | Device -> relay, 50 Hz | Best effort; no ACK/retry. Failed generation/send consumes its sequence. Ordinary 64-entry replay window accepts gaps/reordering. Missing samples can fail capture gates. |
| RFID_EVENT | Device -> relay | Firmware retains one encoded frame, including after a rejected first notify. Same bytes/SEQ retry at 0.5, 1.5, 3.5, 7.5 s after initial attempt; exhaust at 15.5 s. Relay ACKs successful first delivery and exact authenticated duplicates; weapon effect occurs once. New weapon selection supersedes old pending event and ACK identity. |
| COMBAT_FEEDBACK | Relay -> device | Relay ReliabilitySender retries identical frame bytes: demo YAML base 200 ms, x2, +/-20% jitter, five attempts. Device ACKs first application and cached duplicate without repeating its feedback log/effect. Exhaustion logs retransmit/link_degraded/combat_feedback_undelivered; it does not stop sensor ingest. Current demo feedback is a logged stand-in, not a verified actuator. |
| ACK | Either direction | Fresh sequence for every ACK generation; matches only the referenced pending reliable seq. No ACK of ACK and no ACK retries. Lost ACK is recovered by re-ACKing the reliable packet's duplicate. |
| HELLO / HELLO_ACK | Device initiates | Bounded request/response, no separate generic ACK. New attempts use a new device nonce. Duplicate/restart HELLO after initial negotiation ends that session and causes reconnect rather than validating it against old sequence history. Version mismatch refuses session; firmware validates nonce echo and response length. |
| TIME_SYNC_REQ / RESP | Device initiates | Bounded t0-echo exchange; firmware now fails incomplete/mismatched sync rather than falsely declaring success. Whole handshake restarts after loss. Streaming requests receive RESP, not a generic ACK. Demo still initiates only the initial eight exchanges. |

Reliable delivery has an independent monotonic extended-sequence watermark. This admits an unseen
RFID retry even when hundreds of sensor sequences have overtaken it. Exact previously authenticated
ciphertexts are cached (16 entries, 20 seconds) for re-ACK; eviction/expiry never reapplies an old
effect. Older unseen reliable events below the reliable watermark are superseded. Ordinary traffic
keeps the 64-entry replay window. Retry budgets must remain within the cache horizon.

A BLE notify failure before the air is observable: the installed Arduino BLE implementation invokes
ERROR_GATT on failed esp_ble_gatts_send_indicate and SUCCESS_NOTIFY synchronously on stack acceptance.
The existing callback design was checked against that installed source and retained. SUCCESS_NOTIFY
does **not** prove delivery at the laptop. Failed sensor frames increment dropped counters and are
not retried. Failed RFID notifications retain the already sealed frame for byte-identical retry.
There is no re-encryption under the same nonce after failure.

The requested 7.5–15 ms connection interval, latency 0, supervision timeout 4 s are unchanged.
They are requests; the central decides actual parameters. No rate reduction, faster loop, or
speculative BLE timing modification was made. Actual two-link scheduling/fairness/negotiated
parameters remain hardware checks.

Confirmed code defects fixed: silent timeout; classifier blocking BLE ACKs; missing upstream
supervision; frame loss across coalesced reads; incomplete capture contamination; mutable weapon
lookup; future-state combat contamination; replay-state mutation before authentication; firmware
unsigned previous-epoch underflow; first failed RFID never retried; asymmetric duplicate semantics;
reliable retries falling below the sensor replay floor; RX mailbox overwrite; unchecked time-sync
failure; stale session work; Unity old-socket/reconnect races; concurrent WS path replacement race.

## E. Combat time and position contract

All POSITION, WEAPON_CHANGED and ACTION (including guard state) inputs now enter the same
timestamp-ordered buffer after channel conversion. Equal-time weapon/position updates precede ACTION;
equal-time actions retain arrival order. Events older than an already released timestamp are dropped,
not used to roll state backward. The existing late-age limit is also retained. Excessively future
timestamps (>2 s ahead) or full buffers are rejected visibly.

- Network reorder tolerance: `shared/config/match.yaml: reorder_delay_ms = 60`.
- Intentional gameplay grace: `guard_grace_ms = 50`, inclusive.
- The release hold is their sum (110 ms), so the engine can observe guard activation at T+50 with
  60 ms of reorder tolerance. They are separate settings and separate rules.
- The resolver sees the chronological state at attack T plus only the permitted guard-activation
  lookahead. Later weapon, position, heading or guard-release state is not applied early.
- Already guarding at T blocks if geometry permits. A guard entered in [T-150,T] can parry under the
  existing 150 ms rule. A new guard at T+50 may BLOCK but never PARRY; T+51 cannot retroactively block.
- Attacks use the captured ACTION weapon for move validity, range, damage, cooldown and recognition/
  combat messages. SWORD at T=1000 still resolves as SWORD after MACE at T=1020.
- Geometry is the latest ordered measurement at/before T, with the existing 1000 ms maximum age.
  No future position is borrowed to make an earlier attack resolvable.

POSITION reuses existing `playerId,timestamp,x,y,heading`: metres; one calibrated arena origin;
+x = Unity world +x, +y = Unity world +z; heading degrees [0,360), zero +x, positive toward +y.
Unity adaptation occurs only at SendUnityPosition: heading=(90-yaw) mod 360. Future positioning
should output this same structure, with timestamp meaning measurement time in a stable declared
sender clock domain. No real positioning or position echo was built.

**Clock limitation:** initial device->relay conversion estimates t1-t0 without knowing one-way BLE
delay. Its processing-delay proxy is not an RTT measurement. Engine first-arrival channel anchoring
adds another unknown delay, independently for each player/channel. There is no periodic drift
correction currently wired up, despite configuration/helper fields suggesting one. Relative event
deltas on a stable channel are preserved, but cross-player/cross-channel physical accuracy has not
been established and cannot be certified as <50 ms from this protocol. Logs expose sync sample
spread and explicitly label both estimates as unmeasured accuracy. No grace/reorder rule silently
compensates for this. Hardware measurement is required before treating near-boundary outcomes as
physically fair; millisecond unit tests prove the converted-time rule only.

## F. Demo risks / rollout

1. Deploy updated relay code before the updated firmware; new same-sequence RFID retries require
   the new duplicate re-ACK receiver. Updated relay also tolerates legacy fresh-sequence RFID retries
   by suppressing duplicate/stale application timestamps.
2. Rebuild the active Unity APK for reconnect and heading fixes. Preserve its existing scene and
   serialized network settings; do not run the generator.
3. Expect about 50 ms more engine holding time with the explicit grace default, and different
   guard-facing results after yaw correction. Synthetic avatars must face each other for guard tests.
4. Incomplete captures are now rejected rather than filled with untagged data. More `too_few` reports
   reveal a transport problem; do not relax gates as a response.
5. Classifier timeout still uses the existing 120 ms budget and now closes the link for recovery.
   If the classifier regularly exceeds it, collect evidence; this pass did not raise the threshold.
6. No hardware was flashed, no APK deployed, and no physical BLE/tunnel/phone behavior was verified.

Reset command, from comms/ on the engine machine with its existing environment:
```sh
python -m cg_comms.tools.reset_match --config configs/engine.yaml --host localhost
```
Use the existing certificate hostname if it differs from localhost. The command uses the existing
bearer token and TLS settings, accepts only RESET_MATCH on /control, and waits for the reset snapshot.
The shared prototype bearer authorizes control; it is not a newly introduced independent admin role.

## G. Validation

- Baseline Python suite: 165 passed.
- Final Python suite: 218 passed, with two pre-existing WebSocket deprecation warnings.
- Fault tests use real localhost TCP/WebSocket servers: unavailable startup, three close/restart
  cycles for each player/link, blackhole timeout, cancellation ownership, bounded outage admission,
  retained upstream connections through repeated BLE Session failures, concurrent path replacement,
  authorised reset broadcast and rejected credentials.
- Full protocol simulations: both connection orders, concurrent 40-frame tagged P1/P2 windows,
  P1 board-session restart while P2 continues, unchanged upstream registrations.
- Timing tests: before/inside/exactly-on/outside grace; reversed arrival order; permutations of
  attack/weapon/position events; captured weapon; late-drop; reset; cardinal heading/bearing.
- Protocol tests: silence/invalid input deadlines; batched reads; restart HELLO after 200 sequences;
  auth failures do not consume sequence; late reliable first delivery; duplicate re-ACK/no repeated
  effect; no ACK of ACK; generated-but-dropped nonce uniqueness; player isolation; BLE queue/write
  behavior; slow classifier does not stop ACK processing.
- Firmware builds: PlatformIO firebeetle32 and esp32dev_p2 succeed. Production GCM is compiled against
  the installed ESP32 mbedTLS. No flash/upload was run.
- Firmware native execution: blocked by missing host gcc/g++. PlatformIO native_test was attempted
  and failed at compiler invocation. Added reliability tests and extended sequence tests compile
  as Xtensa objects; this is a syntax/type check, **not execution**. Native mode excludes ESP32 GCM
  implementation and does not purport to validate AES on hardware.
- Active Unity runtime scripts compiled successfully with the installed Unity/.NET reference
  assemblies. This is not an APK build or Android runtime test.
- Python compileall and Python 3.10 grammar check (52 modules) pass; runtime tests ran in the
  installed Python 3.14 environment, not on the Ultra96's Python 3.10 image. Focused Ruff correctness
  rules and all new test/helper modules pass.
  Full Ruff retains existing style debt (baseline 115, latest 107 findings); mypy retains 61 errors
  (same count as baseline, principally existing import/type debt). No broad lint/type cleanup.
- Existing compressed soak tests are included; these do not replace sustained physical streaming.

Reproduction commands, with the corresponding existing virtual environment activated:

```sh
# From comms/
python -m pytest -q -p no:cacheprovider
python -m ruff check --select E9,F63,F7,F82 src tests
python -m ruff check src tests
python -m mypy
# From repository root, in the PlatformIO environment
python -m platformio run -d firmware_demo -e firebeetle32 -e esp32dev_p2
python -m platformio test -d firmware_demo -e native_test
```

## H. Physical demo verification

Use INFO logs/JSONL for this check; WARNING-only console mode hides successful correlation lines.
Keep both firmware serial consoles, both relay terminals, classifier, engine, and both phones visible.
The checklist below is a procedure to run on hardware, not a record of completed hardware tests.

A successful action chain is:
`[capture] start trigger_id=N frames=40` (board) ->
`capture_started` -> `window_ready player_id=P trigger_id=N request_id=R window_timestamp_ms=S` ->
`classifier_request ... request_id=R` ->
`ultra96_sim_classified player_id=P trigger_id=N request_id=R` (classifier simulator) ->
`action_classified ... request_id=R window_timestamp_ms=S` ->
`action_received player_id=P source_timestamp=S engine_timestamp_ms=T` (engine) ->
`combat_resolved engine_timestamp_ms=T` or `action_not_resolved`.
IDLE guard can produce STATE_ONLY; absent/stale positions can produce IGNORED while recognition still
reaches the phones. Existing ACTION lacks requestId, so source timestamp bridges the last hop.

| Step | Physical action and exact expected observations |
|---|---|
| 1. Normal P1/P2 | GPIO4 on each: serial `[weapon] now ... accepted=1`, then `[weapon] rfid_event acked seq=...`; relay `weapon_changed player_id=...`. Each GPIO25/26/27 capture follows the chain above and produces one recognition on each phone. |
| 2. P1 first, P2 second | Start P1 board/relay, then P2. For each: `ble_connected mtu_payload=...`, `timesync_seeded`, `session_streaming player_id=...`; serial `[handshake] complete`; engine `ws_client_connected path=/relay/1` and `/relay/2`. P1 continues after P2 joins. |
| 3. P2 first, P1 second | Repeat in reverse order. Same player/path identity and healthy action chain. Compare both boards' generated/accepted/drop counters rather than assuming connection order is harmless. |
| 4. Sustained simultaneous streams | Run both for at least 10 minutes, with regular captures. Serial approximately `[tx] sensor_frames=50/s dropped=0/s ... generated=50/s notify_dropped=... rx_dropped=0`. Counts use actual elapsed reporting intervals, so small scheduling variation is possible. Look for persistent loss or asymmetry. |
| 5. Near-simultaneous triggers | Trigger both boards repeatedly close together. Expect separate playerId/requestId chains; IDs may be numerically equal across players, which is valid. No cross-player trigger/weapon/result mix. |
| 6. Kill/restart classifier | Relays: `classifier_disconnected` -> `classifier_reconnecting delay_s=1/2/4/8`; windows during outage show `window_dropped`. Board serial keeps streaming, engine paths stay registered. Restart only classifier: `classifier_connected`, then a new capture reaches `action_classified`. A silent break can first show `ultra96_timeout request_id=...`. |
| 7. Kill/restart engine | Relays: `engine_disconnected`, `engine_reconnecting`; actions may show `engine_message_dropped reason=offline`. BLE/classifier keep working. Restart: `engine_connected`; engine `engine_starting`, `ws_server_listening`, and client-connected paths for relays/phones. Both phones show fresh HP/presence, then new recognition. |
| 8. Break/restore SSH forwarding | Break the existing relevant tunnel, without changing endpoint configuration. Affected classifier/engine links log the same timeout/disconnect/backoff sequence above; phone HUD reports disconnected/reconnecting. Restore: `classifier_connected`/`engine_connected` and phone `connected`; BLE should have no new handshake solely because an upstream tunnel broke. |
| 9. Restart one relay | Engine logs that relay path disconnected/reconnected. Board logs `[ble] disconnected, advertising again` then `[handshake] complete`; new relay logs `session_streaming`. Held weapon is reannounced. Other player continues without re-handshake. After >64 packets, repeat to exercise old replay-window history. |
| 10. Power-cycle one board | Its relay logs `reconnecting`, possibly preceded by `heartbeat_stale`/`link_lost` or `session_failed`. Recovery logs `ble_connected`, `timesync_seeded`, `session_streaming`; other player's chain continues. Press GPIO4 after reboot. Silence/failed handshake instead produces `handshake_timeout expected=HELLO/TIME_SYNC_REQ`, then retry, never a stuck wait. |
| 11. Restart/background one phone | Engine logs `ws_client_disconnected path=/visualizer/P`, then `ws_client_connected`. Foreground HUD shows `connected` and fresh MATCH_STATE. Rapidly repeat; persistent `ws_duplicate_path_replaced` flapping is a failure, though one replacement of a stale socket is legitimate. |
| 12. Attack/guard timing | Put avatars in range and facing each other. Guard well before attack: BLOCK/PARRY per timing. After allowed grace: HIT; inside grace: BLOCK (no late PARRY). Observe `action_received.engine_timestamp_ms` and `combat_resolved.outcome`. Buttons alone cannot establish a 1 ms physical boundary; record synchronized instrumentation for T+50/T+51 checks. Unit tests cover those boundaries in converted time. |
| 13. Weapon around attack | Start a SWORD capture, switch to MACE during classification/queued resolution. Follow source timestamp/request chain: the first `window_ready weapon=SWORD` must yield SWORD recognition/combat; subsequent capture uses MACE. |
| 14. Counters/notification pressure | Compare serial `generated`, `sensor_frames`, `dropped`, cumulative `notify_dropped`, `rx_dropped` for both orders. Failed RFID sends may show `accepted=0`, then `rfid_event retransmit attempt=...`, then `acked`; one received feedback gives one `[feedback] applied seq=...` despite lost ACK retries. Use a controlled loss rig if available; do not infer radio delivery from stack acceptance. |
| 15. Recovered health + reset | After each fault run several fresh captures. No persistent `window_suppressed`, `stream_decode_drop reason=replay`, `retransmit`, `link_degraded`, `combat_feedback_undelivered`, or `ble_receive_overflow`. Run reset: engine `match_reset_requested` then `match_reset`; CLI and both phones receive `reason=reset`; active phone feed shows `> MATCH RESET`. Verify HP 100/100 and no client reconnect solely due to reset. |

If a whole capture is lost, the relay cannot invent its trigger metadata and may log no capture at
all; compare the board's capture line and counters. Native/unit tests cannot prove controller queue
capacity, actual negotiated intervals, CCCD behavior, adapter fairness or Android background timing.

## I. Remaining findings, prioritized

| Priority | Evidence classification | Deliberately unchanged / required next step |
|---|---|---|
| Before demo acceptance | Hardware validation required | Run H on the real Windows adapter/two boards/phone tunnels after paired deployment. No host simulation validates BLE stack/radio fairness or notification delivery. |
| Before relying on close guard decisions | Strongly supported risk | Initial one-way/first-arrival clock conversion has unknown cross-player error and drift. Measure it; 50 ms grace is an explicit game rule, not a claimed synchronization accuracy. |
| Low-risk pre-demo follow-up | Environment limitation | Install/use a host C++ toolchain on a suitable development machine and run native_test. Updated tests compile for Xtensa, but native execution remains unverified here. |
| After demo | Confirmed prototype limitation | 32-bit random session/device nonce domains and Session-local HELLO nonce history do not give persistent anti-replay/nonce-collision guarantees across process restarts. A stronger handshake/session-key design needs a coordinated protocol change; no credentials were rotated. Failed transmissions themselves do not reseal/reuse a nonce. |
| After demo | Confirmed prototype limitation | No active periodic clock sync, device millis wrap handling, or drift correction; resync/divergence configuration is not a working scheduler. Very long-lived (>49-day device uptime) sessions need a defined wrap strategy. |
| After demo | Confirmed configuration debt | Shared match.yaml owns reorder/grace; legacy engine.yaml reorder field is only a mirror (mismatch now warns). Some legacy backpressure config fields are not wired to the new fixed bounds. |
| After demo | Confirmed delivery limitation | No application ACK/replay of ACTION across engine restart; no persistence/match-generation protocol. In-flight work crossing a restart cannot be globally identified as belonging to an old match. Reset rejects known pre-reset timestamps, but unanchored/new channels have no such historical certainty. |
| After demo | Confirmed demo semantics | Guard remains the last classified guard state until another ACTION updates it; button release is not a guard-release event. Equal-timestamp attacks retain arrival-order tie breaking. Real classifier guard confidence/expiry policy remains unchanged. |
| After demo | Confirmed scope boundary | Real positioning and position echo remain unimplemented. The old Unity scaffold remains unmodified and must not be used as the active build. Existing committed credential migration, Ruff/mypy debt and WebSocket deprecated typing imports remain deferred. |
| Leave unchanged until measured | Speculative | Further BLE rate/interval/latency changes or looser capture gates. No evidence justifies them. |

The final review followed firmware encode/notify/write callbacks, transport queues/framing,
authentication/replay/ACKs, capture windowing, classifier correlation, engine registration/order/
resolution/reset/broadcast, and the active Unity receive/send/reconnect paths. Findings above
distinguish proven code behavior from hardware assumptions.
