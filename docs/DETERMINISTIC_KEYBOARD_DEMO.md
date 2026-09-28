# Deterministic keyboard assessment and the existing GPIO demo

For transmission-speed trials and the complete professor-facing live/video script, use
[COMMS_LIVE_AND_VIDEO_GUIDE.md](COMMS_LIVE_AND_VIDEO_GUIDE.md).

2026-09-28. This revision follows your updated instruction: random dummy selection, a modifying
board transform, and random AI events are no longer required for the keyboard demonstration.
The earlier implementation followed the original assessment brief's explicit requirements for
those three behaviours; they were demonstration fixtures, not requirements of BLE or classification.

## Physical baseline and scope

Your attached **“CG4002 Communications Architecture — Testing Completed So Far”**, dated 28 September,
is the hardware baseline. It reports successful two-player 50 Hz streaming, a bounded 75-second
trial with 74.779 seconds common coverage, keyboard-to-Unity delivery, decoded packet inspection,
1037-byte/33-chunk file upload and RAM readback with CRC/SHA-256 verification, controlled ACK loss,
and individual board/relay/classifier/engine/Unity restart recovery.

It does **not** establish actual out-of-range recovery, simultaneous 100/200 Hz performance,
the highest stable rate, or completion of the newly revised editable-keyboard demonstration.
The new code's software checks below do not turn those into completed physical tests.

Normal GPIO capture/mapping, continuous 50 Hz scheduling, file payloads/readback, `L` fault injection,
`D` packet sampling, connection management, timing profiles, endpoints and credentials are retained.
One additive display change forwards accepted weapon selections to Unity immediately, including
physical selections, using its existing `WEAPON_CHANGED` message handler.

## Controls

| Key | Operation |
|---|---|
| `a` | Ask the board to cycle its held weapon: NONE → SWORD → SPEAR → MACE → SWORD… |
| `b` | Send fixed editable variant **B** to the board; verify identity return; classify returned IMU |
| `c` | Same operation with fixed variant **C** |
| `d` | Same operation with fixed variant **D** |
| `F` | Existing file upload, CRC, RAM readback, byte/SHA-256 verification and saved returned file |
| `L`, then `F` | Existing deliberate one-upload-ACK-loss demonstration |
| `D` | Toggle sampled normal `SENSOR_FRAME` display; distinct from lowercase `d` |
| `Q` | Stop keyboard listening; streaming continues |
| Ctrl+C | Stop this relay process |

Other characters do not send assessment packets. Keys are never queued. Wait for an operation to
finish; `BUSY`, `OFFLINE`, `NO_WEAPON` and `UPSTREAM_OFFLINE` are explicit rejections. `a` needs a
streaming board and engine connection, but no prior weapon and no classifier connection. B/C/D
require a selected weapon and both upstream connections. File operations only require the board.

The physical action buttons remain independent. Keyboard `b/c/d` do not become GPIO trigger IDs
1/2/3 and do not edit the GPIO trigger map. Keyboard `a` and physical GPIO4 share the board's held
weapon, so either can advance the same existing round robin. The relay does not invent a separate
weapon sequence. A retransmitted `a` command must not cycle twice.

## What traverses the pipeline

```text
GPIO action button -> existing 50 Hz triggered capture -> normal multi-sample SENSOR_WINDOW --+
                                                                                        |
keyboard b/c/d -> fixed raw IMU -> encrypted BLE -> board identity reply                   |
             -> verify returned fields -> scale actual return -> one-sample SENSOR_WINDOW-+
                                                                                        |
                            same classifier TCP client -> ACTION_RESULT                  |
                            -> same relay confidence/correlation checks                  |
                            -> ACTION over engine WebSocket -> ACTION_RECOGNIZED -> Unity
```

The keyboard request still contains the complete real 42-byte sensor body inside its assessment
envelope. The board returns the raw sensor axes unchanged; relay conversion is the normal
accelerometer `/2048` g and gyroscope `/16.4` deg/s conversion. Timestamp/correlation metadata still
changes per transaction. This is **not** an encrypted-byte loopback or a direct action command.

The keyboard fixture remains a one-sample-per-hand `SENSOR_WINDOW`, with `triggerId=null` and a
`demoTraceId`. It does not claim to be a physical 40-sample gesture capture. The ordinary classifier
schema carries dominant/offhand samples; torso is verified in the BLE body but not classified.
Continuous sensor frames and the physical capture window builder run independently.

### Identity transform and protocol compatibility

The previous implementation had only transforms 1=negate, 2=add-keycode and 3=swap/scale.
There was no existing identity mechanism. Revision 3 adds **0=IDENTITY** for request keys b/c/d.
There is no separate transform field in the request: the key selects identity in both firmware
and Python; the reply explicitly reports `transform_id=0`, which the relay verifies along with
all returned sensor fields. Legacy arithmetic IDs remain available for low-level protocol fixtures.

`a` adds two small assessment packet types: `ASSESS_WEAPON_REQ` 0x46 (4-byte trace ID) and
`ASSESS_WEAPON_RESP` 0x47 (trace ID, device time, selected weapon; 9 bytes). They use the existing
authenticated codec, serialized operation lock, identical-request retries and board response cache.
The board invokes the existing GPIO weapon-cycle routine and announces its selection through the
existing RFID path. The correlated response can also confirm selection if that announcement is lost.
An expired/cancelled response cannot create a new action or cycle a weapon again.

Base wire version remains 1; demo firmware reports **firmware version 3**. The largest frame remains
76 bytes. Update Python and flash both boards together. New keyboard operations reject firmware
older than version 3 with a clear message. File formats are unchanged.

## Classifier behaviour: explain it honestly

`--random --seed 7` still means random choices from a seeded sequence. Identical B/C IMU inputs
can produce different actions; the seed does not make classification a function of the input.

The new optional `--assessment-deterministic` mode applies only to traced keyboard windows with no
GPIO trigger ID. It computes SHA-256 over a canonical representation of the **received dominant
samples, offhand samples and weapon**, then selects an index in that weapon's sorted valid move list.
It does not inspect the key, variant ID, player, timestamps, request number or trace when choosing.
Confidence is 1.0 and guard state is IDLE. This is a transparent **communications simulator**, not
trained inference or a claim that a hash understands gestures.

With unchanged rule data and the same weapon, identical classified IMU content produces the same
full input fingerprint and action regardless of B/C key or player. Different inputs may select the
same action because the move list is small; matching action names alone do not prove matching input.
Use the received sample and input fingerprint as evidence. Changing weapons can change the action.
Equal classifications do not promise equal combat outcomes: cooldown, positions, guard, HP and
match status still govern resolution.

Use the existing **trigger map plus the new flag** to support both paths concurrently. Ordinary
physical GPIO windows keep their existing trigger-map classification. The old random/scenario
modes remain available and retain their behaviour when the new flag is omitted. The trace field
is a demo-routing convention, not an authentication boundary.

## Install and start

Pull the commit containing this revision on the relay laptop and Ultra96 from each repository root:

```bash
git pull --ff-only origin main
```

Existing editable Python installs pick up the source; there are no new dependencies or entry points.
Use the actual runtime laptop repository path (your earlier logs used `C:\Users\Qian Hui\Desktop\CG4002`).
Keep existing machine-local YAML paths when those contain your working BLE addresses.

During preparation, from `firmware_demo/`, flash the normal 50 Hz targets with your existing ports:

```powershell
& "..\pio-venv\Scripts\pio.exe" run -e firebeetle32 -t upload --upload-port COM8
& "..\pio-venv\Scripts\pio.exe" run -e esp32dev_p2 -t upload --upload-port COM7
```

Use the board targets matching your actual hardware. These are the established targets you supplied.
Then disconnect USB from the relay laptop and return to the separate power supplies.

Rebuild/install the active Unity project at `visualizer_demo/CG4002VisualizerDemo_UnityTemp/My project`
for the full weapon-selection feed improvement, with correct P1/P2 IDs. Existing reviewed APKs
already understand `WEAPON_CHANGED` and can update the weapon label; the rebuilt APK also logs each
selection in the visible feed. The B/C/D action trace display remains the existing implementation.
Keep scene settings, phone 127.0.0.1:8080/UseTls=false and both Termux SSH/socat commands.

On **Ultra96**, from `comms/`, two terminals:

```bash
.venv/bin/cg-engine --config configs/engine.yaml --latency-profile assessment
```

```bash
.venv/bin/cg-ultra96-sim --port 8888 --trigger-map configs/scenarios/demo_trigger_map.yaml --assessment-deterministic
```

Stop the previous classifier using port 8888 first. This replaces the earlier `--random --seed 7`
command for this demonstration. Do not start both classifier processes.

On the **relay laptop**, keep the existing SSH forward alive. From `comms/`, separate terminals:

```powershell
.\.venv\Scripts\cg-relay.exe --config configs\relay.p1.yaml --no-tunnel --assessment --latency-profile assessment --console-level INFO
```

```powershell
.\.venv\Scripts\cg-relay.exe --config configs\relay.p2.yaml --no-tunnel --assessment --latency-profile assessment --console-level INFO
```

`--assess-seed` is still accepted for old command compatibility but no longer affects keyboard data.
The paired assessment profile remains the existing 500 ms classifier deadline / 600 ms base engine
hold + 50 ms grace. It applies to all actions in those processes. No default timing was changed.

## Live demonstration: deterministic editing

1. Wait for both sessions, classifier/engine connections and phones. Press `a` in the focused P1
   relay console. Show `assess_weapon_sent`, `weapon_changed` / `assess_weapon_verified`, engine
   `weapon_selected`, and the Unity weapon label/feed. Cycle until SWORD. Repeat on P2.
2. Reset the match with the existing command below and wait two seconds for positions. As before,
   resets do not cycle the physical board weapon; if the HUD was cleared, `a` can reselect it.
3. Press `b`, then `c`, then `d`, allowing four seconds between action keys to make combat outcomes
   easy to interpret. Default SWORD examples are B=VERTICAL_SWING, C=HORIZONTAL_SWING, D=THRUST.
   These examples depend on the shipped values and move list, not a hardcoded key-to-action table.
4. On the **running relay laptop**, open `comms/src/cg_comms/relay/assess_dummy.py`. Change only B's
   values from `(2048, -1024, 512)` to C's `(1000, -2000, 3000)`. Keep B's ID/name as `1, "B"`.
   Save the file. **Do not restart the relay or classifier and do not reflash.**
5. Press `b`. The console must still say `key=b variant=B`, now with C's previous values. Show the
   identical board return, converted sample and matching classifier input ID/action from C.
   Follow this transaction's own new trace to Unity. Trace IDs should differ between transactions.
6. Press `c` to compare again. Both use the same IMU values and selected weapon. Restore B's original
   tuple and save when finished; the next `b` uses the original values again.
7. Press the physical action buttons and show the old trigger IDs/trigger-map actions. Editing B
   must not change those GPIO actions. Continue using normal physical press/release spacing.

```bash
.venv/bin/python -m cg_comms.tools.reset_match --config configs/engine.yaml --host localhost
```

Keep each tuple to three integer literals in -32768..32767. Keep all three IDs/names and table
structure. Parsing only literal table entries avoids executing an edited expression during live
reload. A bad or partially saved file rejects the key with `assess_echo_failed`; fix/save and press
again. Already in-flight transactions retain their original values for verification. An edit on
the development PC does not automatically edit the separate relay laptop's filesystem.

## Terminal evidence

| Stage/event | Essential evidence |
|---|---|
| `assess_echo_sent` | key, B/C/D, raw selected acceleration, IDENTITY, transaction trace |
| `assess_echo_verified` | actual returned acceleration, transform=0, verification event, BLE RTT |
| `assess_pipeline_forward` | converted dominant/offhand samples, selected weapon, request ID |
| `classifier_request` / `classifier_response` | same trace/request, deadline and elapsed exchange |
| `ultra96_sim_classified` | received IMU, deterministic mode, input fingerprint, action |
| `action_classified` | correlated classifier action forwarded by the normal relay path |
| engine `action_received` / `combat_resolved` | normal engine admission and outcome |
| Unity | recognized action with matching `[P1-…]` / `[P2-…]` trace |

Console views abbreviate `demo_trace_id` to `trace`, round displayed IMU units to six decimals,
and show the first 16 SHA-256 hex characters as `input_id`. The JSONL files retain full field names,
precision, full SHA-256 and exchange timing. Raw integer values are not rounded. Failures retain
their full diagnostic fields. P1/P2 colours and `D` packet visibility are unchanged.

`imu_g_dps` and `offhand_g_dps` are ordered `[ax, ay, az, gx, gy, gz]`. The first three are g,
the last three deg/s. `assess_echo_verified` alone still proves only the board round trip; complete
delivery needs the matching phone trace. `ON_COOLDOWN`, BLOCK, guard and post-match suppression
remain gameplay outcomes, not evidence that a key chose another dummy variant.

## Focused hardware regression after deployment

This is a new revision check, not a claim that your already-passed baseline needs reclassification.

- Verify `a` cycles correctly, then alternate `a` and GPIO4 to prove shared weapon state.
- Demonstrate repeated B/C/D selection and the live B := C edit through physical boards and Unity.
- Check both normal 50 Hz streams continue during keyboard operations; use `D` to inspect them.
- Repeat your already-working `F` and `L` then `F`, comparing the returned file's SHA-256.
- Spot-check a board and a process restart, then a fresh `a/b` transaction. Preserve any failures.
- Actual out-of-range and 100/200 Hz/max-stable-rate tests remain separate unfinished baseline items.

For the original button-only demo, use your normal relay and engine commands without assessment
flags/profile and the original classifier `--trigger-map` command without the new flag. Normal
50 Hz firmware still supports those physical buttons. Keep the existing tunnel and phone commands.

## Local validation

- Full Python suite: **385 passed**, two existing WebSocket deprecation warnings (62.46 seconds).
- Both normal firmware targets (`firebeetle32`, `esp32dev_p2`) compile successfully. Compilation
  does not execute the physical BLE handlers; no board was flashed by this change.
- Tests cover live B := C editing, seed-independent key selection, identity/extreme values,
  malformed edit rejection, weapon round robin, lost ACK/reply recovery without double cycling,
  preservation of a newer physical selection, cancellation, old-firmware rejection, ordinary
  trigger-map routing, and normal sensor reception during keyboard/file operations with ACK loss.
- Integration tests exercise simulated board -> real localhost classifier TCP -> real engine
  WebSocket -> simulated phone receipt, including weapon display messages and action traces,
  with both configured timing and the delayed-classifier assessment profile.
- All original 15 binary golden vectors remain unchanged; two new weapon-command vectors were
  generated with the independent reference encoder and tested by the normal codec tests.
- Ruff correctness checks across source/tests and full checks on the focused keyboard/classifier/
  logging modules and new tests pass. Changed Python parses with Python 3.10 syntax.
- Configured mypy remains non-clean: **61 errors in 14 files**, the previously documented count.
  This revision does not claim a clean repository-wide type check.
- No Ultra96 execution, Android build/install, new physical recovery test or RF measurement was
  performed by the agent. Your earlier physical baseline remains separate from these results.
