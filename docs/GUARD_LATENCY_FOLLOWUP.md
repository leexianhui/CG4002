# Focused guard, classifier latency, and late-event follow-up

2026-09-22. Based on the supplied 677-record hardware log attachment. Hardware retesting and APK
deployment have not been performed by the agent. This pass changes no firmware, secrets, endpoints,
wire schemas, timeout defaults, or combat timing defaults.

## Changes and guard verification

- Active `Demo.unity`: only the two avatar quaternion rotations and their Euler hints were edited
  relative to the user's existing scene. P1 Y=90 degrees; P2 Y=270 degrees. Existing position, network,
  UI and rendering edits are preserved. No scene regeneration.
- Actual path: `DemoHudController.Update` reads the selected avatar's world x/z and Unity yaw;
  `EngineClient.SendUnityPosition` converts heading as `(90 - yaw) mod 360`; POSITION enters the
  engine's ordered state; the resolver compares defender heading with `atan2(dy, dx)` to the attacker.
  With P1 x=0 and P2 x=1, headings must be P1=0, P2=180 in engine coordinates.
- Previously both yaw values were zero: both engine headings were 90, a 90-degree facing error.
  The guard arc is 120 degrees total (60 degrees each side), so both directions correctly gave HIT.
- Tests cover the converted POSITION -> state -> guard -> resolver path in both directions using
  the real shared rules. They reproduce the old HIT and verify BLOCK (damage 3 for sword horizontal
  swing), PARRY (damage 0), inclusive +50 ms grace BLOCK, and +51 ms HIT. No guard-rule change needed.
- `combat_resolved` now reports the actual resolution snapshot's defender guard state, entry time,
  heading, bearing to attacker, facing error, facing boolean, and guard age. A negative guard age
  means grace looked forward to a later activation. Repeated guard classifications do not reset an
  already-active guard's entry time; the next non-guard action or match reset clears guarding.

## Classifier investigation and recommendation

The old run had 72 windows, 67 requests, 62 accepted classifications and five deadline misses.
Successful request-to-classified log intervals had medians of about 20 ms and maxima of 110/118 ms.
The five timeouts occurred around 121-134 ms. These are censored observations: a timeout does not
tell us when the reply would eventually have reached the relay.

There is no intentional delay in the trigger-map simulator. It performs a lookup, JSON encode,
write and drain. All five lost requests were classified on the server. Comparing the old wall-clock
logs suggests delays on both sides of that result log: roughly 110 ms before it for three timeouts,
and roughly 126-130 ms after it for two P2 timeouts. Independent host clocks and the absence of a
server receive log prevent reliable one-way measurement or attribution to a specific component.
The latter cases especially do not support blaming inference compute alone. Network/SSH forwarding,
server scheduling, relay scheduling and synchronous console/file logging remain candidates.

The confirmed loss mechanism is the 120 ms deadline followed by connection closure and no replay.
The physical cause of the tail is **not established** by the old logs. No speculative transport fix
or timeout increase was applied.

New measurements:

- Relay `classifier_response`: monotonic `elapsed_ms`, `encode_ms`, `write_drain_ms`,
  `response_wait_ms`, request byte count, window timestamp/age and timeout budget.
- `ultra96_timeout`: the same fields plus phase and deadline overrun. Overrun can include event-loop
  scheduling/cleanup delay; it is not by itself proof of a specific stall.
- Simulator `ultra96_sim_classified`: monotonic `processing_ms`, `write_drain_ms`, `service_ms`.
  Processing starts after a full line is returned by readline; time waiting for that line or for the
  server task to run is outside this measurement. Drain completion is not an on-wire delivery ACK.
- `classifier_probe`: a separate classifier-only connection, 40 synthetic samples per hand, high
  request IDs, no engine connection or ACTION forwarding. Its 500 ms diagnostic timeout observes
  tails beyond 120 ms without changing gameplay settings. Summary percentiles are successful calls
  only, so always inspect timeout/unavailable counts too. `request_call_ms` includes local logging;
  relay `elapsed_ms` isolates the timed exchange. No new dependencies.

**Recommendation:** retain 120 ms while taking the first measurement run. If service/encoding/drain
are small but the two-player SSH-path tail still exceeds 120 ms, a reasonable controlled trial is
200 ms classifier timeout on both relays, paired with engine `reorder_delay_ms: 250` in
`shared/config/match.yaml`. Keep `guard_grace_ms: 50`, yielding a 300 ms total engine hold.
This is a trial, not a claim that the existing logs prove 200 ms sufficient; collect at least 100
samples per player and actual gameplay `event_age_ms` before accepting it. If the probe times out
at 500 ms or has large tails, investigate the route/scheduling rather than repeatedly raising limits.

Why paired: the engine rejects events older than its released watermark even if they are inside the
500 ms age allowance. A longer classifier timeout alone can accept a reply which the engine then
drops, or accept a guard after the attack was resolved. For a guard at attack time T+grace, the
normalized arrival age must fit inside the base reorder delay to arrive before T+grace+reorder.
The 250 ms trial allocates 200 ms to classification and a provisional 50 ms to other delays; check
actual `event_age_ms` and clock-anchor uncertainty. The trial adds 190 ms hold versus today's 110 ms.
Unit tests demonstrate a 200 ms delayed guard failing with the original hold and succeeding with
the trial hold; they do not establish physical cross-player clock accuracy.

## Late-event diagnostics

`event_dropped` / `action_dropped_late` now include `player_id`, `message_type` (ACTION, POSITION or
WEAPON_CHANGED), `reason`, `source_timestamp`, `engine_timestamp_ms`, arrival engine time, event age,
release watermark, queue depth, release delay and the aggregate drop count. Reasons are:

- `before_release_watermark`
- `max_age_exceeded`
- `future_timestamp`
- `buffer_full`

The existing `event_before_match_reset` also includes type/times and `reason=before_match_reset`.
Admission, ordering and drop policies are unchanged. The legacy `total_dropped_late` counter still
aggregates all reorder rejections; use `reason` for the actual cause.

UTC `timestamp` remains the log timestamp. Numeric engine event time now uses `engine_timestamp_ms`
and relay window time uses `window_timestamp_ms`, avoiding the logger's timestamp collision.
`source_timestamp` in action_received equals the relay's window timestamp. This fixes diagnostics;
wire message timestamps and resolver time calculations were not changed.

## Exact hardware retest

Deploy this follow-up by updating the Python code on the relay laptop and Ultra96,
then restart their relays, engine and classifier. If using a file transfer instead of a Git update,
the following archive contains just the changed runtime Python files (run in the development
repository root in PowerShell):

```powershell
tar -czf comms/logs/guard-followup-runtime.tar.gz comms/src/cg_comms/engine/game_state_task.py comms/src/cg_comms/engine/reorder.py comms/src/cg_comms/relay/session.py comms/src/cg_comms/relay/ultra96.py comms/src/cg_comms/mock/ultra96_sim.py comms/src/cg_comms/tools/classifier_probe.py
```

Transfer that archive to each runtime machine and extract from its existing CG4002 repository root:
`tar -xzf /path/to/guard-followup-runtime.tar.gz`. Existing editable installs see the updated source.
Keep the existing working configs, BLE addresses, credentials, certificates and tunnels.

1. In the active Unity project, reload `Assets/Scenes/Demo.unity` from disk if the editor prompts.
   Verify **Player1Avatar** Y rotation 90 and **Player2Avatar** Y rotation 270, with original positions.
   Build/install P1 and P2 APKs with their respective EngineClient PlayerId. Keep phone
   127.0.0.1:8080 / UseTls=false and existing Termux SSH+socat. No firmware flash is needed.
2. On Ultra96, from its `comms/`, start each in its own existing terminal/tmux pane:

   ```bash
   .venv/bin/cg-engine --config configs/engine.yaml
   .venv/bin/cg-ultra96-sim --port 8888 --trigger-map configs/scenarios/demo_trigger_map.yaml
   ```

3. Keep the existing laptop SSH forwards alive. From relay-laptop `comms/`, one terminal per relay:

   ```powershell
   .\.venv\Scripts\cg-relay.exe --config configs/relay.p1.yaml --no-tunnel --console-level INFO
   .\.venv\Scripts\cg-relay.exe --config configs/relay.p2.yaml --no-tunnel --console-level INFO
   ```

   Substitute your existing `logs/relay.pN.local.yaml` paths if those hold the working BLE addresses.
   Wait for both `session_streaming`, `engine_connected` and `classifier_connected`, and both phones.
4. Select SWORD on both boards with GPIO4 (cycle until relay reports SWORD). Reset on Ultra96:

   ```bash
   .venv/bin/python -m cg_comms.tools.reset_match --config configs/engine.yaml --host localhost
   ```

   Use the existing cert hostname if different. Expect `match_reset` and HP 100/100. Wait two seconds
   for both POSITION feeds after reset. Do not press other buttons during the controlled trial.
5. Press P2 GPIO27 once. Wait for its guard recognition (`action_received guard_state=GUARDING`,
   followed by STATE_ONLY / phone GUARD). Wait another 0.5 seconds, then press P1 GPIO25 once.
   Expect `combat_resolved outcome=BLOCK damage=3 defender_guard_state=GUARDING`,
   `defender_heading_deg=180`, `bearing_to_attacker_deg=180`, `facing_error_deg=0`,
   `facing_attacker=true`, and P2 HP 97. Repeat after reset with P1 guarding/P2 attacking;
   defender heading/bearing should both be 0, and P1 HP 97.
6. Reset; wait for positions; attack without guarding: expect HIT damage 10. Reset between parry
   attempts. PARRY needs a newly-entered guard 0..150 ms before attack in normalized engine time;
   guard 1..50 ms after attack can BLOCK only. Use the new timestamps/guard age to interpret results,
   not physical button timing or line-print order. Same-board extra presses during the ~0.8 s
   capture are still ignored, so space normal presses at least 1.2 seconds apart and release fully.
7. With services, relays, BLE and tunnels running, pause button presses. In two separate laptop
   terminals from `comms/`, run the diagnostic probe concurrently (same config-path caveat as above):

   ```powershell
   .\.venv\Scripts\python.exe -m cg_comms.tools.classifier_probe --config configs/relay.p1.yaml --samples 100 --timeout-ms 500 --interval-ms 1000
   .\.venv\Scripts\python.exe -m cg_comms.tools.classifier_probe --config configs/relay.p2.yaml --samples 100 --timeout-ms 500 --interval-ms 1000
   ```

   This sends synthetic classification requests; it cannot change HP or guard state. Save both JSON
   summaries and `logs/classifier-probe-p*.jsonl`, plus classifier logs. Match high request IDs.
   Large server service time points to server work/scheduling during handling; small service time
   with large relay response wait points to transport or scheduling outside that interval. Large
   encode/write time points to relay work/local backpressure. These metrics narrow, not perfectly
   partition, delay. An optional same-host comparison can run the same module on Ultra96 with its
   `.venv/bin/python` and a config whose classifier host is 127.0.0.1:8888.
8. Resume normal alternating and near-simultaneous two-player actions for ten minutes. Collect
   relay RTT/window age, engine action age, and every drop record. If a controlled 200/250/50 trial
   is justified by step 7, edit both local relay timeout values and shared match reorder value,
   restart relays+engine, and repeat steps 4-8. Do not change staleness/span gates or guard grace.
   Pass: reliable early-guard BLOCK in both directions, no unexplained missing actions, and no
   sustained timestamp rejections. Preserve any remaining rejected records with all new fields.

## Verification

- 30 new tests; 65 focused tests passed (new follow-up, prior combat boundaries, reorder tests).
- Full Python suite: **248 passed**, two existing WebSocket deprecation warnings, 27.02 seconds.
- Focused Ruff correctness checks across src/tests pass; full Ruff on the new probe/tests and
  modified classifier client/reorder module passes.
- Existing configured mypy check still reports 61 errors in 14 files, matching the prior baseline;
  broad import/type cleanup remains out of scope.
- Scene comparison against a pre-pass snapshot verifies exactly four changed rotation lines and
  quaternion-derived yaw 90/270; other scene edits remain intact. No C# script or firmware changes.
- Probe CLI help and a real localhost delayed-server test pass. The test reproduces a 120 ms
  deadline failure and measures the same 180 ms response with the diagnostic deadline, without
  changing the live configuration or forwarding actions to the engine.

This pass does not claim on-device Android/BLE testing or a measured bound on physical cross-player
clock synchronization. Rebuild/install the APKs and run the hardware steps above.
