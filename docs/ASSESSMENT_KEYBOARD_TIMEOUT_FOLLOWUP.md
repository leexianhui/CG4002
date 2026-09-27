# Missing assessment keyboard actions: hardware-log investigation

> **Keyboard update, 2026-09-28:** use [DETERMINISTIC_KEYBOARD_DEMO.md](DETERMINISTIC_KEYBOARD_DEMO.md)
> for the current a=weapon / b,c,d=fixed IMU mappings, identity return, live edits and classifier command.
> Earlier random-key instructions below are historical. Timing-profile and unrelated test procedures remain applicable.

2026-09-24. Based on the supplied two-run attachment (relay P1/P2, classifier, engine and throughput
records). The user tested keyboard input on P1 only. This follow-up changes Python timing-profile
selection and diagnostics; no firmware, wire payload, secret, tunnel or phone code changes.

## What the logs establish

| Stage | Evidence across both runs |
|---|---|
| Keyboard -> physical board -> relay | 9 sent, 9 verified; returned transform correct; BLE round trips 26–38 ms |
| Relay -> Ultra96 classifier | All 9 requests were classified; simulator service time 0.896–1.309 ms |
| Classifier reply accepted by relay | 4 replies accepted, measured exchanges 8.745–12.107 ms |
| Reply discarded at relay deadline | 5 timeouts, reported elapsed 123.348–133.326 ms against the 120 ms budget |
| Engine | The 4 accepted classifications reached the engine; 3 HITs and 1 ON_COOLDOWN |
| Phone | Engine logs alone do not prove what was rendered; check the matching HUD trace during retest |

Second run, P1 traces `P1-f5b238dd-N`:

| Trace suffix / request ID | Result |
|---|---|
| 1 / 0 | Classified HORIZONTAL_SWING; engine HIT |
| 2 / 1 | Classifier timeout; no ACTION forwarded |
| 3 / 2 | Classified CHARGED_ATTACK; engine HIT |
| 4 / 3 | Classified CHARGED_ATTACK; engine HIT |
| 5 / 4 | Classified CHARGED_ATTACK; engine ON_COOLDOWN |
| 6 / 5 | Classifier timeout; no ACTION forwarded |
| 7 / 6 | Classifier timeout; no ACTION forwarded |
| 8 / 7 | Classifier timeout; no ACTION forwarded |

The first run's one keyboard request also timed out. These are deadline losses, not missing BLE
keypresses or failed board transforms. `assess_echo_verified` verifies only the board round trip.
The relay intentionally closes the classifier connection after timeout and does not replay old actions.

The timeout observations are censored: 133 ms is when timeout/cleanup was logged, not the eventual
reply's arrival time. The two machines' wall clocks differ; subtracting their printed UTC times does
not produce reliable one-way latency. Small measured server service time excludes most inference work
as the explanation, but leaves transport/SSH buffering and scheduling outside the measured interval,
including relay scheduling. This attachment does not identify the physical source of the tail.

## Fix and its tradeoff

Added **`--latency-profile assessment`** to both relay and engine commands. It is explicit and must
be selected on the engine and both relays together:

- Relay classifier deadline: **500 ms**.
- Engine base reorder hold: **600 ms**, with the existing **50 ms** guard grace, total **650 ms**.
- Normal defaults/configuration remain unchanged when the profile is not selected.
- No timestamp rewriting, duplicate action replay or relaxed confidence/capture gates.
- The existing age/future/queue rejection logic remains active. The profile changes the base hold
  used by that logic; it does not accept arbitrarily late messages.

The 100 ms between deadline and base hold is a provisional allowance for other normalized arrival
delays. This profile trades 540 ms additional engine hold for more classifier tolerance during the
communications demonstration. It is a bounded mitigation for the confirmed 120 ms deadline loss,
not proof that this network's tail is below 500 ms. It applies to all traffic in those selected
processes; use the original commands for normal button gameplay.

A longer classifier deadline alone is insufficient: ongoing POSITION events can advance the engine's
release watermark and cause a delayed ACTION to be rejected, or a guard can arrive after resolution.
The paired hold preserves timestamp ordering rather than pretending an old event occurred on arrival.
Physical cross-player clock accuracy is still unmeasured.

Also added `demo_trace_id` to classifier request/response/timeout and engine ACTION admission/drop logs.
An expired classification now produces an explicit `window_dropped reason=classifier_timeout` with
that trace, so a successful BLE exchange cannot be mistaken for complete pipeline success.

## Update and rerun

Pull the latest `main` on the relay laptop and Ultra96, from each existing repository root:

```bash
git pull --ff-only origin main
```

Use the current working repository paths on each machine. The attachment's relay laptop belongs to
`C:\Users\Qian Hui\Desktop\CG4002`, not the development machine's `T0332075` path. Run the following
service commands from that machine's existing **`comms/`** directory. Keep the existing working local
config paths if they contain your BLE addresses. Stop the old relays and engine before restarting.

Ultra96 engine:

```bash
.venv/bin/cg-engine --config configs/engine.yaml --latency-profile assessment
```

Ultra96 classifier (unchanged):

```bash
.venv/bin/cg-ultra96-sim --port 8888 --random --seed 7
```

Relay laptop P1:

```powershell
.\.venv\Scripts\cg-relay.exe --config configs\relay.p1.yaml --no-tunnel --assessment --latency-profile assessment --assess-seed 11 --console-level INFO
```

Relay laptop P2:

```powershell
.\.venv\Scripts\cg-relay.exe --config configs\relay.p2.yaml --no-tunnel --assessment --latency-profile assessment --assess-seed 22 --console-level INFO
```

Existing editable installs see the new source. No new dependency or entry point is required.
**No board reflash or APK rebuild is needed for this follow-up** if already on the reviewed `fabc789`
firmware/apps. Keep your laptop SSH and both phones' SSH/socat commands unchanged. Keep both boards
at normal 50 Hz for this pipeline retest.

1. Confirm `relay_starting latency_profile=assessment classifier_timeout_ms=500` on both relays.
2. Confirm engine `assessment_timing_profile reorder_delay_ms=600 release_delay_ms=650` with the
   current 50 ms grace. Both sides print a reminder to pair the profile; this is not automatic
   cross-machine configuration negotiation.
3. Confirm both BLE sessions, classifier/engine connections and phones. Select SWORD on both boards.
4. Reset the match on Ultra96 with the existing reset tool, then wait two seconds for positions:

   ```bash
   .venv/bin/python -m cg_comms.tools.reset_match --config configs/engine.yaml --host localhost
   ```

5. On P1, press `a`, `b`, `c`, allowing **at least four seconds** between keys. Repeat on P2.
   Follow each trace: `assess_echo_verified` -> `classifier_response` / `action_classified` ->
   engine `action_received` -> phone ACTION_RECOGNIZED trace. Expect the deliberate engine hold.
6. Random charged attacks have cooldowns up to 3.5 seconds in the current weapon table. A recognized
   action with `ON_COOLDOWN` is delivered but does not damage HP. Reset before HP reaches zero;
   post-match suppression is also distinct from classifier packet loss.
7. Repeat several rounds and save both relay and Ultra96 logs. Pass only when each accepted key
   reaches the phone or has an explicitly explained outcome. Retain any timeout/drop records.

To return to the previous button demo: stop these processes and use your original relay commands
without the assessment/profile flags, your original engine command without the profile flag, and
the classifier `--trigger-map configs/scenarios/demo_trigger_map.yaml`. No YAML timing files were edited.

## Measure the remaining tail rather than guessing its cause

With both relays/tunnels running, pause key and GPIO actions. Run the existing diagnostic probes in
two separate laptop terminals from `comms/`:

```powershell
.\.venv\Scripts\python.exe -m cg_comms.tools.classifier_probe --config configs\relay.p1.yaml --samples 100 --timeout-ms 500 --interval-ms 1000
```

```powershell
.\.venv\Scripts\python.exe -m cg_comms.tools.classifier_probe --config configs\relay.p2.yaml --samples 100 --timeout-ms 500 --interval-ms 1000
```

Save summaries and classifier logs. Probe connections do not forward actions or change HP. They
exercise synthetic windows, so compare their timing with real keyboard `classifier_response` records
too. A 500 ms timeout is still a failed observation; do not silently exclude it from percentile claims.
If tails remain large, compare a same-host Ultra96 probe to the SSH route and investigate scheduling,
terminal/logging and transport. Do not repeatedly increase limits without measurements. If latency
is stable and bounded after a route fix, a tighter paired budget can be evaluated separately.

## Other findings in this attachment

**The throughput streams are healthy in these runs:** both boards show about 50 Hz and 16.8 kbps
payload goodput each, with zero observed sequence gaps. First-run durations are approximately 336 s
and 320 s. Sequence gaps are still not a proof of zero possible loss outside the observed span.

The pasted dashboard has `[FINAL]` for both relays. `TOTAL 0 ... INCOMPLETE` correctly reports that
neither is currently running; it does not erase their completed run averages. For the live evidence,
start the dashboard while both relays are still streaming and let it finish before stopping them:

```powershell
.\.venv\Scripts\python.exe -m cg_comms.relay.throughput --duration-s 75 --output logs\assessment\keyboard-followup-rate50.json
```

**P2 phone POSITION delays are a separate issue:** the first engine run records 23 late P2 POSITION
rejections with normalized ages 243–1926 ms. Those are not the five lost keyboard ACTIONs; the latter
never left the relay. The profile does not promise to recover multi-second phone stalls. Keep the
phone app foreground and both Termux processes running, and retain new position-drop records for
route/scheduling investigation. Do not disable timestamp checks or count a dropped position as a pass.

**Validation:** regression tests reproduce a 250 ms reply failing at 120 ms and succeeding at 500 ms,
with a 650 ms reply still rejected. A delayed guard test proves why the engine profile is required.
The full local simulated-board -> delayed TCP classifier -> WebSocket engine -> visualizer test uses
ongoing phone POSITION traffic and a preseeded action clock, so a trivial first-arrival clock anchor
cannot hide a missing reorder hold. Physical retesting remains to be performed by the operator.

Full Python suite: **350 passed**, two existing WebSocket deprecation warnings. Ruff correctness
checks across source/tests and full checks on the new timing module/tests pass. Python 3.10 syntax
checks and both CLI help commands pass. The configured mypy baseline remains non-clean (61 errors
in 14 files); the package's existing missing `py.typed` import issue is not repaired in this fix.
