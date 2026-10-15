# CG4002 Comms Demo — Triggered-Action Plan (P1 first, P2-symmetric)

**Status:** planning only. Nothing in this document has been implemented. Written from a full read of
the working tree at commit `faa32e1` plus the untracked `log.txt` capture.

**Scope of evidence.** Every claim about behaviour below is traced to source. `log.txt` is a capture
collected by another machine's processes (its relay log path is
`C:\Users\Qian Hui\Desktop\CG4002\comms\logs\...`), pasted into this checkout; it is used only as
corroboration of what the source already says, and is labelled where cited. No process was run on the
development PC for this plan.

---

## 1. VERIFIED CURRENT ARCHITECTURE

### 1.1 The chain that exists today

```
FireBeetle (comms/firmware/, ESP32)
  │  BLE GATT, custom binary frames (8-byte header, AES-128-GCM, CRC-16/CCITT-FALSE)
  │  SENSOR_FRAME 0x10 @50 Hz, HEARTBEAT 0x20 @1 Hz, RFID_EVENT 0x11 on button press
  ▼
Player Relay (comms cg-relay, Qian Hui's laptop)
  │  relay/session.py    _handle_frame dispatch -> _handle_sensor_frame / _handle_rfid_event
  │  relay/windowing.py  PlayerWindower.ingest_frame: 40-sample ring, emit every 5 frames
  │  relay/ultra96.py    line-delimited JSON over one TCP conn, correlate by requestId, 120 ms timeout
  ▼                                                   ▲
Ultra96 classifier stand-in (cg-ultra96-sim, :8888)   │  ACTION_RESULT
  │  mock/ultra96_sim.py: itertools.cycle(scenario steps), window contents ignored
  ▼───────────────────────────────────────────────────┘
Player Relay
  │  relay/engine_link.py send_action -> ACTION (+ WEAPON_CHANGED)
  │  WSS /relay/1, bearer auth
  ▼
Game Engine (cg-engine, Ultra96, :8443)
  │  engine/ws_server.py  paths /relay/{1,2} + /visualizer/{1,2}, ConnectionRegistry
  │  engine/inbound.py    route_inbound -> InboundAction / InboundWeaponChanged / InboundPosition
  │  engine/game_state_task.py  single writer, per-(player,channel) clock anchor, ReorderBuffer
  │  engine/resolver.py   pure resolve() -> IGNORED / STATE_ONLY / ON_COOLDOWN / MISS / HIT / BLOCK / PARRY
  │  engine/broadcaster.py broadcast_to_visualizers / send_to_relay
  ▼
Unity Android visualizer (visualizer_demo/CG4002VisualizerDemo_UnityTemp/My project/)
     Net/EngineClient.cs -> Controllers/DemoHudController.cs
```

### 1.2 What actually reaches a visualizer today

`comms/src/cg_comms/engine/app.py` wires exactly three callbacks:

| callback | destination |
|---|---|
| `on_combat_event` | `broadcast_to_visualizers` |
| `on_match_state` | `broadcast_to_visualizers` |
| `on_combat_feedback` | `send_to_relay` |

Consequences, all confirmed in source:

- **Only `COMBAT_EVENT` and `MATCH_STATE` ever reach a phone.**
- `WEAPON_CHANGED` is routed *in* from the relay (`inbound.py`) and applied to `PlayerState.weapon`,
  but is **never forwarded** to a visualizer. `DemoHudController.HandleWeaponChanged` therefore can
  never fire.
- `POSITION` is consumed but **never echoed**, contradicting `shared/protocol/app_messages.md` §2.3
  ("Position is echoed, not just consumed").
- `COOLDOWN_STATE` is **never produced** by any code path.
- There is **no full-state snapshot on visualizer connect**, contradicting `app_messages.md` §2.5
  ("as part of the full-state snapshot a visualizer receives immediately on connect"). A phone that
  connects sees nothing until an HP change or a wire-visible combat outcome occurs.

### 1.3 The two root causes of "one press → endless attacks"

**Cause A — the relay windows unconditionally.** `PlayerWindower.ingest_frame`
(`comms/src/cg_comms/relay/windowing.py`) gates emission on four things only: stride boundary
(`_frame_count % stride_frames`), ring fullness, `_current_weapon is not None`, and the
staleness/span quality checks. With `relay.p1.yaml` `stride_frames: 5` at `frame_rate_hz: 50.0`, that
is **one SENSOR_WINDOW every 100 ms, forever**, from the first confirmed weapon onward. Nothing in
the relay is aware of player intent.

**Cause B — the classifier stand-in ignores the window.** `mock/ultra96_sim.py:_handle_client` builds
`cycle = itertools.cycle(steps)` once per connection and answers *every* `SensorWindow` with
`next(cycle)`. The window's samples, weapon, timestamps, and `stale_pct` are never read.
`comms/configs/scenarios/demo_scenario.yaml` is a 6-step cycle
(IDLE, HORIZONTAL_SWING 0.94, IDLE+GUARDING, THRUST 0.88, IDLE, VERTICAL_SWING 0.91), so at 10
windows/s the rig produces roughly **three attacks per second** with no further input.

The `log.txt` capture agrees: 96 `window_ready`, 94 `action_classified`, spaced ~100 ms, alternating
IDLE / HORIZONTAL_SWING / THRUST / VERTICAL_SWING, from a single weapon-button press.

### 1.4 The third defect: nothing was ever going to appear in Unity

`engine/resolver.py` step 7 returns `IGNORED` when either player's `position` is `None` or older than
`position_max_age_ms: 1000`. `GameStateTask._resolve_one` then hits:

```python
if decision.outcome in (CombatOutcome.IGNORED, CombatOutcome.STATE_ONLY, CombatOutcome.ON_COOLDOWN):
    ...
    logger.info("action_not_resolved", ...)
    return          # <- no outbound message of any kind
```

`GameStateTask.__post_init__` always creates `PlayerState` for players 1 *and* 2, so `defender` is
never `None`; it is the **position** check that fails. With no visualizer sending POSITION, every
non-IDLE action is `IGNORED` and every IDLE is `STATE_ONLY`, and **neither emits anything**.

`log.txt` corroborates exactly this: 94 `action_not_resolved` (46 `IGNORED` + 48 `STATE_ONLY`),
**zero** `combat_resolved`, **zero** `COMBAT_EVENT`. One `ws_client_connected` for `/relay/1` and no
visualizer ever attached.

**So the current protocol genuinely cannot express "P1 performed HORIZONTAL_SWING".** That is a
finding, not an opinion: there is no message type, and no producer site, for action recognition
independent of combat resolution.

### 1.5 Structures that already exist and should be reused, not rebuilt

| Need | What already exists | Where |
|---|---|---|
| Player presence | `ConnectionRegistry._connections` keyed by the four paths; already logs `ws_client_connected` / `ws_client_disconnected` with the path | `engine/ws_server.py` |
| Classifier-agnostic relay | `relay/ultra96.py` docstring: relay "never knows which one it's talking to"; correlation by `requestId`, confidence gate outside the client | `relay/ultra96.py` |
| Config-over-constants | `WindowingConfig`, `Ultra96Config`, `EngineLinkConfig`, all YAML-backed and logged at boot | `comms/src/cg_comms/config.py` |
| Trigger-carrying wire space | `CG_FLAG_MOTION_ACTIVE (1u<<2)` **defined and unused**; flag bits 5–7 explicitly "reserved: must be 0 on send, ignored on receive"; free packet types 0x12–0x1F; spare `reserved` byte in `RFID_EVENT` | `shared/protocol/cg_protocol.h`, `protocol/payloads.py` |
| Flags already plumbed to the windower | `Session._handle_sensor_frame` already passes `flags=decoded.fields["flags"]` into `ingest_frame` | `relay/session.py` |
| Declarative payload layouts | `PAYLOAD_TABLES` — one table per packet type, the only `struct` consumer | `protocol/payloads.py` |

That last-but-one row is the single most important structural fact in this plan: **a trigger reaches
the relay as a backward-compatible extension of existing wire *semantics*, not as a new wire format**
— because `flags` is already a transmitted field, is already decoded, and is already handed to the
windower.

Stated precisely, so nobody over-claims it in a review:

- **Unchanged:** packet size and layout, the header, the packet-type set, nonce derivation, AES-128-GCM
  and the tag, the CRC, the reliability/retransmit path, the `SENSOR_FRAME` payload *structure*, and
  every golden vector.
- **Changed:** the *meaning* assigned to bits that the protocol already reserves — bit 2
  (`CG_FLAG_MOTION_ACTIVE`, defined and previously always 0) and bits 5–7 (documented as
  "reserved: must be 0 on send, ignored on receive"). A receiver built before this change ignores them
  and behaves exactly as it does today, which is what makes the extension backward-compatible.

So: **no new packet type, no payload-table row, no golden-vector churn, no crypto change** — but it is
a wire-semantics extension, not literally "zero wire changes".

---

## 2. CURRENT BUTTON / FIRMWARE BEHAVIOUR (verified)

`comms/firmware/src/main.cpp` is 387 lines; the relevant part is `loop()` (line 338):

```cpp
uint32_t now = millis();
if (now - last_sensor_ms >= CG_DEMO_SENSOR_FRAME_INTERVAL_MS) { last_sensor_ms = now; send_sensor_frame(); }
if (now - last_heartbeat_ms >= CG_DEMO_HEARTBEAT_INTERVAL_MS)  { last_heartbeat_ms = now; send_heartbeat(); }
bool button_down = digitalRead(CG_DEMO_WEAPON_BUTTON_PIN) == LOW;
if (button_down && !button_was_down) { send_rfid_event_next_weapon(); }
button_was_down = button_down;
drain_incoming_non_blocking();
```

Findings:

1. **There is exactly one button**, `CG_DEMO_WEAPON_BUTTON_PIN 4` (`include/cg_config.h`), wired
   `INPUT_PULLUP`, active-LOW, documented as "an RFID-reader stand-in (cycles SWORD -> SPEAR -> MACE
   on each press)".
2. **It is not a motion trigger.** `send_rfid_event_next_weapon()` does
   `g_weapon_id = (g_weapon_id % 3) + 1` and sends `CG_PKT_RFID_EVENT` — a *weapon/equipment*
   event. `NONE` is never selected, which is correct (a tag pickup always yields a real weapon).
3. **There is no debounce.** Only edge detection (`button_down && !button_was_down`). A real
   push-button's contact bounce can produce two edges milliseconds apart. This is a latent bug in the
   *existing* weapon button and must be fixed once for all buttons.
4. **SENSOR_FRAME is unconditional and its IMU content is synthetic.** `send_sensor_frame()` advances
   a `static float phase += 0.3f` per frame and emits a sine/cosine sweep; `flags` is hardcoded `0`;
   `offhand` and `torso` are constant 1 g on Z. There is no MPU-6050 in this firmware at all. The
   `torso` block is sent and then never read by the relay.
5. **`drain_incoming_non_blocking()` must keep running.** It ACKs `CG_PKT_COMBAT_FEEDBACK`; without
   it the relay retransmits 5× and logs `link_degraded` + `combat_feedback_undelivered`.
6. **Build note.** `platformio.ini` has `env:firebeetle32` and a dev-bench `env:esp32dev`
   (`extends = env:firebeetle32`, board `esp32dev`), plus `-I../shared/protocol` so the firmware
   compiles the *normative* `cg_protocol.h` rather than a copy.

Consequence for the demo: because the IMU is synthetic, **no amount of relay or Ultra96 work can make
the current firmware's signal content distinguish one gesture from another.** The gesture identity has
to come from which button was pressed. §3 and §5 keep that fact from turning into "the FireBeetle
decides the action".

---

## 3. ONE-TRIGGER → ONE-CLASSIFICATION DESIGN

### 3.1 The four candidate designs, evaluated

| | Design A: firmware captures a finite burst and only sends those frames | Design B: button sends a trigger packet; relay takes the next N frames | Design C: IMU streams continuously; relay suppresses classification until a trigger arrives, then emits exactly one window | Design D: use an existing mechanism as-is |
|---|---|---|---|---|
| Wire changes | Firmware stops streaming between captures → changes the *shape* of the link, breaks `max_span_deviation_pct` reasoning and the mock scenarios | New packet type 0x12 + `PAYLOAD_TABLES` entry + `RELIABLE_PACKET_TYPES` entry + `constants.py` + session dispatch branch + firmware send path + a golden vector | **None.** `flags` bit 2 (`CG_FLAG_MOTION_ACTIVE`, already defined, currently unused) marks capture frames; bits 5–7 (explicitly reserved) carry the trigger id | — |
| Relay changes | Windowing must tolerate long gaps; span gate must be relaxed or bypassed | Dispatch branch + windowing arm/countdown | Windowing arm/countdown only; `flags` is **already** passed to `ingest_frame` | — |
| One-press → one-window guarantee | Fragile: the first frames after a gap fail the span check, so the *first* capture after idle is often dropped | Good, but the trigger is a single packet — reliable, so retransmit adds latency and an out-of-order trigger is possible | **Strongest.** Every frame of the capture carries the tag, so the arm survives dropped frames (40× redundancy) on a best-effort channel | — |
| Multi-button | Needs a button id in a new field anyway | Natural (`button_id` in payload) | Natural (3 bits = ids 1–7, enough for 7 buttons) | — |
| Presentability | Hard to narrate: the link goes quiet | Clean to narrate | Clean: "the belt says *these* 800 ms are a swing attempt" | — |
| Future real classifier | Poor: a real game wants continuous windows | Fine | **Best:** continuous 10 Hz windowing is *preserved* and re-selected by one config flag | — |
| Effort | High | Medium | **Low** | — |
| Verdict | reject | reject | **RECOMMEND** | see below |

**Design D (already naturally supported) does not exist.** I checked every candidate: `RFID_EVENT`
drives weapon only and reaches the windower as `on_weapon_confirmed`; `min_action_confidence` gates
confidence, not rate; `max_in_flight: 2` throttles concurrency, not cadence; `stride_frames` only
changes the *rate* of the endless stream. There is no existing switch that turns continuous
classification into per-press classification.

### 3.2 The recommended design (Design C), precisely

**Firmware.** Per action button: debounce (≥200 ms), and on a confirmed falling edge set
`capture_frames_remaining = CG_DEMO_CAPTURE_FRAMES` (40 = one full window) and
`capture_trigger_id = <that button's id>`. In `send_sensor_frame()`, while
`capture_frames_remaining > 0`, set

```
flags = CG_FLAG_MOTION_ACTIVE | ((capture_trigger_id & 0x7) << 5)
```

and decrement. Outside a capture, `flags = 0` exactly as today. Frame rate, packet type, payload
layout, and crypto are untouched. A new press during a capture is ignored (the capture is atomic),
which is what makes one press yield one window even if the player double-taps.

**Relay.** `PlayerWindower` gains `trigger_mode: bool`. When `trigger_mode` is `False` (the default),
behaviour is bit-for-bit what it is today — continuous stride emission — so every existing test,
the mock rig, and the future real-game path are unaffected. When `True`:

- keep appending every frame to the rings (so the window covers the whole motion, including the
  pre-trigger tail already buffered);
- on the first frame with `CG_FLAG_MOTION_ACTIVE` set while disarmed: arm, record
  `trigger_id = (flags & 0b111_00000) >> 5`, set `_capture_remaining = window_samples`;
- while armed, decrement `_capture_remaining` per frame; when it reaches 0, run the **existing**
  fullness / staleness / span / weapon gates and emit **one** `SensorWindow` carrying `trigger_id`;
- then disarm and enter a refractory state that ignores `MOTION_ACTIVE` until at least one untagged
  frame has been seen. Firmware guarantees untagged frames between presses, so this is belt-and-braces
  against a stuck-low button.

`stride_frames` is not consulted in trigger mode. The quality gates are deliberately *kept*: a capture
that straddles a stall is still discarded, and the operator sees a `window_suppressed` log rather than
a confident wrong classification.

**Ultra96 side.** `SENSOR_WINDOW` gains an optional `triggerId`. The sim maps
`(weapon, triggerId) → action` from a YAML table that lives **on the Ultra96 side** (§5). The relay
never interprets `triggerId`; it copies it.

### 3.3 Why this does not make the FireBeetle classify — and the exact words to use

**State this plainly, in these terms, and do not overstate it.** The honest description of what the
communications demo does:

1. **The FireBeetle sends IMU samples plus trigger metadata.** Per press it marks a bounded run of
   `SENSOR_FRAME`s as one capture attempt and tags them with a trigger identifier. It sends
   *"frames N..N+39 are one capture attempt, and the input that started it was trigger 2"* — a fact
   about the player's input, not a conclusion about the motion.
2. **`cg-ultra96-sim` deterministically maps that metadata to an action.** The decision function is a
   weapon-keyed lookup table (`demo_trigger_map.yaml`) that lives on the Ultra96 side.
3. **The FireBeetle never sends an `ActionClass` and therefore does not classify.** There is no gesture
   vocabulary in the firmware, no mapping table in the firmware, and no action name anywhere on the
   BLE wire. The `ActionClass` value is produced for the first time on the Ultra96 side.
4. **The simulator is NOT inferring the gesture from the synthetic IMU waveform.** Say so out loud.
   The firmware's IMU content is a synthetic sine/cosine sweep (§2 finding 4) and the sim does not
   analyse it. Claiming otherwise would be a false claim about the demo, and it is not needed: the
   architectural property that matters is *where the decision is made*, not how sophisticated the
   decision is.
5. **Later, the real Ultra96 classifier will classify from the IMU samples** using the *same*
   `SENSOR_WINDOW` → `ACTION_RESULT` interface, and can ignore `triggerId` entirely. That swap needs no
   change to FireBeetle→relay, relay→Ultra96, relay→engine, or engine→Unity.

The claim this demo does make: **the classification decision is made on the Ultra96 side, behind an
interface a real model can occupy without redesigning any link.** The claim it does not make: that
anything is currently being recognised from motion signal.

**Rejected alternative, recorded deliberately.** The sim could instead classify from signal content
(firmware emits a different synthetic waveform per button; the sim thresholds peak gyro magnitude).
That looks purer but is strictly more code in two places for zero architectural gain, and it buys a
*worse* kind of honesty — a threshold on a waveform the firmware fabricated to be separable is not
gesture recognition either, it just obscures where the table is. The trigger-identifier route keeps the
lookup visible and therefore truthfully describable.

### 3.4 Timing and what the operator will see

- Press → last capture frame: 40 frames × 20 ms = **800 ms**.
- Window ready → `ACTION_RESULT`: one round trip, 120 ms timeout, typically single-digit ms locally.
- ACTION → engine resolve: `reorder_delay_ms: 60`.
- Total press-to-HUD: roughly **0.9–1.0 s**, once, per press. That is a good demo cadence — slow
  enough to narrate, fast enough not to look broken.

Optional tightening if 800 ms feels slow on the day: set `CG_DEMO_CAPTURE_FRAMES` to 30 and
`window_samples` to 30 together (they must match, and `window_samples` feeds `nominal_span_ms`). Do
not change one without the other. Leave both alone unless asked.

---

## 4. MULTI-BUTTON ARCHITECTURE

### 4.1 The two categories, kept separate

The brief's distinction maps onto the code cleanly, and the code already enforces it:

**Category 1 — motion-classified actions.** Reach the engine as `ACTION.action`, an `ActionClass`
value produced by the Ultra96. Path: button → tagged SENSOR_FRAMEs → relay window → Ultra96 decides →
`ACTION_RESULT` → `ACTION` → resolver → Unity.

**Category 2 — discrete non-gesture control events.** Weapon/equipment selection already has its own
first-class path: `RFID_EVENT` → `windower.on_weapon_confirmed` + `WEAPON_CHANGED`. It does **not**
go through IMU classification, and it should not be forced to.

### 4.2 What the action vocabulary actually permits

This is a hard constraint that rules out most of the brief's candidate button list. Verified in
`shared/enums/enums.yaml` and `comms/src/cg_comms/messages/enums.py`:

- `ActionClass` = `IDLE, HORIZONTAL_SWING, VERTICAL_SWING, DIAGONAL_SWING, THRUST, CHARGED_ATTACK, CHARGED_THRUST, SWING`.
- **There is no `RELOAD`, no `GRENADE`, no `SHIELD`.** They exist nowhere in the enums, the schemas,
  the resolver, or `shared/config/weapons.yaml`.
- Guard is not an `ActionClass` at all. It is `GuardState.{IDLE,GUARDING}`, a *continuous state* that
  rides alongside the action in the same message, with its own confidence.
- `shared/config/weapons.yaml` defines which moves each weapon may use:
  - `SWORD`: HORIZONTAL_SWING 10 / 1.5 m / 800 ms, VERTICAL_SWING 12 / 1.4 / 900,
    DIAGONAL_SWING 11 / 1.5 / 850, THRUST 8 / 1.8 / 600, CHARGED_ATTACK 25 / 1.6 / 3000
  - `SPEAR`: THRUST 12 / 2.5 / 700, CHARGED_THRUST 28 / 2.8 / 3200, SWING 8 / 2.0 / 1000
  - `MACE`: VERTICAL_SWING 16 / 1.3 / 1200, DIAGONAL_SWING 15 / 1.3 / 1150, CHARGED_ATTACK 32 / 1.4 / 3500
- `resolver.py` step 5 returns `IGNORED` for a move the held weapon does not have
  (`rules.is_valid_move`). So *"swing button while holding a SPEAR"* is legitimately nothing.

**Therefore: do not invent RELOAD/GRENADE/SHIELD buttons for this demo.** Adding them would mean new
enum members, regenerated `shared/generated/`, new resolver branches, new weapons.yaml semantics, and
a protocol version bump — a game-design change wearing a comms-demo hat. Recorded here as an explicit
rejection of that part of the candidate list.

### 4.3 Recommended smallest sensible mapping

Four buttons, all pins configurable in one place:

| Button | Trigger id | Category | Meaning | Reaches engine as |
|---|---|---|---|---|
| GPIO **4** (existing, unchanged) | — | 2 | Cycle weapon SWORD → SPEAR → MACE | `RFID_EVENT` → `WEAPON_CHANGED` |
| GPIO **25** | 1 | 1 | "capture a motion action" — trigger 1 | Ultra96 decides; default table → `HORIZONTAL_SWING` (SWORD) |
| GPIO **26** | 2 | 1 | trigger 2 | Ultra96 decides; default table → `THRUST` |
| GPIO **27** | 3 | 1 | trigger 3 — **guard** | Ultra96 decides; default table → `action: IDLE` + `guardState: GUARDING` |

Pins 25/26/27 are safe general-purpose GPIOs on both `firebeetle32` and the `esp32dev` bench board
(not strapping pins, not input-only, not flash-attached). Trigger id **4** is left free for a fourth
button (e.g. `VERTICAL_SWING`) with no code change once the table has a row for it — 3 bits give ids
1–7, with 0 meaning "not a capture frame".

Three properties worth stating plainly:

- **Guard stays a demo button, and it is a Category-1 trigger.** Guard state comes out of the
  classifier (`ACTION_RESULT.guardState`), so the guard button routes through the Ultra96 like any
  other motion trigger. Routing it directly from firmware would be the FireBeetle deciding a
  classifier output — the same violation as naming a gesture.
- **Guard is carried as `action: IDLE` + `guardState: GUARDING`, and that shape is load-bearing.**
  `Action`/`ActionResult` carry `action` and `guardState` as two independent fields
  (`messages/models.py:55-64` and `:168-173`), and `GameStateTask._apply_guard_state` applies the guard
  field *before* the action is queued for resolution. **Consequence for §7.2: an emit rule keyed on
  `action != IDLE` would make the guard button the one button that displays nothing.** The rule must be
  `action != IDLE OR guardState != IDLE`. This is the reason that rule changed.
- **Weapon selection stays direct.** It is genuinely not a gesture, the protocol already has a
  first-class path for it, and forcing it through IMU classification would contradict the game design.

### 4.4 Where the mapping is configured

Three separate places, each owned by the machine that should own it:

1. **Which GPIO is which trigger id** — `comms/firmware/include/cg_config.h`, two parallel arrays plus
   a count. Edit and re-flash; no other file changes.
2. **Which trigger id means which action, per weapon** — `comms/configs/scenarios/demo_trigger_map.yaml`,
   read by `cg-ultra96-sim` on the Ultra96. Edit and restart the sim; no re-flash, no relay change.
3. **Whether the relay is trigger-driven at all** — `windowing.trigger_mode` in `comms/configs/relay.p1.yaml`.

The mapping table is weapon-keyed so a press is always a move the held weapon can actually make.
Showing the *other* case on purpose (press a SWORD swing while holding a SPEAR → `ACTION_RECOGNIZED`
with `outcome: IGNORED`) is a genuinely good demo beat: it proves recognition and resolution are
separate stages.

---

## 5. ULTRA96 SIMULATOR CHANGES

### 5.1 What it receives, and what it does with it today

It receives, per request, one line of JSON: a `SENSOR_WINDOW` with `playerId`, `requestId`, `weapon`,
`windowStartTs`, `windowEndTs`, `sampleRateHz`, `dominant` (40 × 6 floats), `offhand` (40 × 6), and
`stalePct`. It replies with one `ACTION_RESULT` carrying `requestId` echoed, `action`,
`actionConfidence`, `guardState`, `guardConfidence`.

Today it reads **only** `timestamp`, `playerId`, and `requestId` from that window. The action comes
from `next(cycle)` over `demo_scenario.yaml`. That is the whole of Cause B in §1.3, and it is why the
attacks are both automatic and unrelated to anything the player did.

### 5.2 The change

Add a `--trigger-map <path>` option. When supplied, the sim classifies from the window's `triggerId`
via a weapon-keyed table; when omitted, it keeps the existing `--scenario` cycle exactly as-is, so
`comms/tests/test_e2e_mock.py`, the soak test, and the mock rig continue to pass untouched.

```yaml
# comms/configs/scenarios/demo_trigger_map.yaml
default:                      # used when the weapon/trigger has no specific row
  action: IDLE
  action_confidence: 1.0
triggers:
  SWORD:
    1: { action: HORIZONTAL_SWING, action_confidence: 0.94 }
    2: { action: THRUST,           action_confidence: 0.91 }
    3: { action: IDLE, action_confidence: 1.0, guard_state: GUARDING, guard_confidence: 0.88 }
  SPEAR:
    1: { action: SWING,            action_confidence: 0.90 }
    2: { action: THRUST,           action_confidence: 0.93 }
    3: { action: IDLE, action_confidence: 1.0, guard_state: GUARDING, guard_confidence: 0.88 }
  MACE:
    1: { action: DIAGONAL_SWING,   action_confidence: 0.92 }
    2: { action: VERTICAL_SWING,   action_confidence: 0.94 }
    3: { action: IDLE, action_confidence: 1.0, guard_state: GUARDING, guard_confidence: 0.88 }
```

Note the shape of the trigger-3 rows: `action: IDLE` **with** `guard_state: GUARDING`. That is how the
existing protocol represents guard (`ActionResult` carries `action` and `guardState` as two independent
fields), and every row must therefore be able to set both. A row that omits `guard_state` defaults to
`GuardState.IDLE`, which is what releases a guard — so pressing trigger 1 after trigger 3 both swings
*and* drops the guard, in one message, exactly as the real classifier would report it.

A window with no `triggerId`, or a `triggerId` with no row, answers `default`: `action: IDLE`,
`guard_state: IDLE`, confidence 1.0. That is the one combination the engine deliberately does **not**
display (§7.2) — it is pure idle, not a recognised action — and it passes the relay's confidence gate
by design (`passes_confidence_gate` always admits IDLE, `relay/ultra96.py:34`).
Every reply logs `ultra96_sim_classified` with `request_id`, `trigger_id`, the chosen action, the chosen
guard state, and the table row that matched, so "why did it say that" is answerable from the log alone.

`comms/configs/scenarios/demo_scenario.yaml` is **kept**, not deleted: it is still the right driver
for the no-hardware mock rig and the e2e tests. Its header comment should gain a line saying it is no
longer the demo's classifier source.

### 5.3 How the real classifier replaces it

The replacement contract is unchanged and already correct:

- Same TCP listener, same line-delimited JSON, same `SENSOR_WINDOW` in / `ACTION_RESULT` out, same
  `requestId` echo, same 120 ms budget, same `max_in_flight: 2`.
- The real model reads `dominant`/`offhand`/`sampleRateHz`/`weapon` and **ignores** `triggerId`.
- On the relay side, set `windowing.trigger_mode: false` and the relay returns to continuous 10 Hz
  windowing — which is what a real model wants. That is one YAML line, on one machine.
- `relay/ultra96.py` is not touched by any of this. Its docstring's promise ("relay code never knows
  which one it's talking to") stays true, which is the property the brief asked to preserve.

The one interface change the real classifier's owner must be told about: `SENSOR_WINDOW` may carry an
extra `triggerId` key. `COMMS_IMPLEMENTATION_PLAN.md` §16.3 rule 3 already requires the *relay* to
ignore unknown keys from the classifier; this is the mirror of that, and it is additive — a model that
ignores it behaves identically.

---

## 6. ACTION RECOGNITION vs COMBAT RESOLUTION

Two different facts, two different stages, two different messages.

| | Action recognition | Combat resolution |
|---|---|---|
| Claim | "P1 performed HORIZONTAL_SWING with a SWORD, confidence 0.94", or "P1 raised a guard" | "P1's HORIZONTAL_SWING hit P2 for 10, P2 now at 90" |
| Decided by | Ultra96 classifier | `engine/resolver.py` |
| Needs an opponent | **No** | Yes |
| Needs positions | **No** | Yes (`position_max_age_ms: 1000`) |
| Needs a weapon that has the move | No | Yes (`is_valid_move`) |
| Needs the cooldown clear | No | Yes |
| Proof today | **none exists** | `COMBAT_EVENT` |
| Proof after this plan | `ACTION_RECOGNIZED` | `COMBAT_EVENT` (unchanged) |

The resolver's seven outcomes split on exactly this line: `MISS/HIT/BLOCK/PARRY` are combat facts and
go on the wire; `IGNORED/STATE_ONLY/ON_COOLDOWN` are *non-resolutions* and today vanish into a log
line. Recognition is upstream of all seven.

**Guard is the sharpest case, and it is why the emit rule is not `action != IDLE`.** A guard press is a
fully successful recognition that the resolver classifies as `STATE_ONLY` — `resolve()` returns at
`resolver.py:62-63` the moment it sees `action == IDLE`, having already had the guard field applied by
`_apply_guard_state`. So guard produces a real classification, a real state change in the engine, and
under the rejected rule **nothing on the wire at all**. Recognition is therefore reported whenever
`action != IDLE OR guardState != IDLE`. An action that was recognised and then not resolved is a
success for the communications subsystem and a no-op for the game — and the demo must be able to say
so out loud.

Concretely: **HP reduction and `COMBAT_EVENT` must not be the proof that the pipeline works**, because
in the P1-only configuration they can never occur, and the pipeline still works. The proof is
`ACTION_RECOGNIZED` carrying the classified action, and `outcome` explaining what the game did with it.

---

## 7. PROTOCOL CHANGES

### 7.1 Can the existing protocol express action recognition? No.

Checked exhaustively against `shared/protocol/app_messages.md` and `messages/models.py`:

- **`COMBAT_EVENT`** — `defenderId` is required and non-null; `outcome` is constrained to
  `WireCombatOutcome` = `MISS/HIT/BLOCK/PARRY`; `app_messages.md` §2.4 states normatively that
  `IGNORED`, `STATE_ONLY`, `ON_COOLDOWN` "never reach the wire". Using it for an unresolved action
  would be exactly the abuse the brief forbids, and would also make every `MISS` animation and HP
  readout in the HUD ambiguous.
- **`MATCH_STATE`** — phase/hp/winner/reason. No action field, no per-player action concept.
- **`WEAPON_CHANGED`** — weapon only, and it is relay→engine, not engine→visualizer.
- **`COOLDOWN_STATE`** — cooldown timers only, and nothing produces it.
- **`POSITION`** — coordinates only.
- All models are `extra="forbid"`, so no message can be quietly extended with an action field at the
  sender without the receiver's model knowing.

So a new message is **necessary**, and it is the minimum: one additive type, no version bump, no
change to any existing message's meaning.

### 7.2 `ACTION_RECOGNIZED` — engine → both visualizers

```jsonc
{ "type": "ACTION_RECOGNIZED", "version": 1, "timestamp": 0,
  "playerId": 1,
  "weapon": "SWORD",
  "action": "HORIZONTAL_SWING",  // ActionClass, may be IDLE when this is a guard-only report
  "actionConfidence": 0.94,
  "guardState": "IDLE",          // GuardState — IDLE or GUARDING
  "guardConfidence": 0.0,
  "outcome": "IGNORED",          // full CombatOutcome vocabulary, all 7 values legal here
  "targetId": null }             // a REAL connected opponent, or null — see the presence rule below
```

A guard press is therefore reported as:

```jsonc
{ "type": "ACTION_RECOGNIZED", "version": 1, "timestamp": 0, "playerId": 1, "weapon": "SWORD",
  "action": "IDLE", "actionConfidence": 1.0,
  "guardState": "GUARDING", "guardConfidence": 0.88,
  "outcome": "STATE_ONLY", "targetId": null }
```

…which Unity renders as `> P1 used GUARD`. No animation, no stance, no visual guard representation.

| Aspect | Decision |
|---|---|
| **Producer** | `GameStateTask._resolve_one`, immediately after `resolve()` returns, **before** the early-return branch for IGNORED/STATE_ONLY/ON_COOLDOWN |
| **When emitted** | Once per inbound `ACTION` where **`action != IDLE` OR `guardState != IDLE`**, regardless of outcome. Exactly one per recognised action, always, including when a `COMBAT_EVENT` also follows |
| **Not emitted for** | **Pure idle only** — `action == IDLE` **AND** `guardState == IDLE`. That is the continuous-mode filler this plan exists to stop displaying; it is a state update, not a recognised action |
| **Why not just `action != IDLE`** | Guard is carried as `action: IDLE` + `guardState: GUARDING`, so that rule would silently drop the guard button — the one demo button that would then display nothing. This is the corrected rule |
| **Guard fields** | `guardState` + `guardConfidence`, copied from the inbound `ACTION` (which copied them from `ACTION_RESULT`). The engine does not re-derive them |
| **Fan-out** | `broadcast_to_visualizers` — both `/visualizer/1` and `/visualizer/2`, so each phone sees both players' actions. Required for P2 symmetry |
| **Difference from `COMBAT_EVENT`** | `ACTION_RECOGNIZED` says *what was recognised* and *what the game did about it*; it carries no damage and no HP. `COMBAT_EVENT` says *what combat happened* and carries damage/HP/range. They are emitted for the same action when it resolves, and only `ACTION_RECOGNIZED` when it does not |
| **P1 only** | Every press produces `ACTION_RECOGNIZED` with `outcome: "IGNORED"` (or `STATE_ONLY` for guard) and `targetId: null` |
| **P1 + P2** | Both phones get P1's and P2's `ACTION_RECOGNIZED`; resolving ones are followed by `COMBAT_EVENT` and `MATCH_STATE` |
| **P2 symmetry** | `playerId` is the only player field; nothing about it is P1-specific |
| **Compatibility** | Additive. `version` stays 1. An old receiver logs `unhandled message type 'ACTION_RECOGNIZED'` and continues — `EngineClient.DispatchMessage`'s `default` branch already does exactly that |

`weapon` is read from the attacker's `PlayerState` (the engine's own confirmed weapon), not from the
inbound message, so it cannot disagree with what the resolver validated the move against.

#### `targetId` must come from relay presence, not from the resolver — a verified trap

**Do not copy `decision.defender_id` into `targetId`.** The resolver's `_other_player_id`
(`resolver.py:46-48`) returns *any* other player id present in `state.players`, and
`GameStateTask.__post_init__` **always constructs both `PlayerState`s** — so with P2 entirely absent,
`decision.defender_id` is `2`, and the position branch (`resolver.py:81-91`) even propagates it into the
`IGNORED` Decision:

```python
return Decision(outcome=CombatOutcome.IGNORED, attacker_id=ev.player_id, defender_id=defender_id)
```

Copying that field through would make Unity announce a target that is not there — the same class of lie
as a fake P2, arriving by accident rather than by design.

**Rule:** `targetId` is non-null **only when that opponent's `/relay/<id>` is actually registered**,
i.e. only when the presence map says so:

```python
opponent = decision.defender_id
target_id = opponent if opponent is not None and presence.get(str(opponent), False) else None
```

Symmetric by construction: the same expression yields `null` for P2's actions while `/relay/1` is
absent. The internal `PlayerState` for an absent player keeps existing (nothing about the state model
changes, which is what keeps P2 plug-in trivial) — it simply never becomes a *claimed target*.

Corollary for M4: a P2 **visualizer** sim is not a relay, so `targetId` stays `null` during that test
even while combat resolves and HP drops. If `targetId: 2` is wanted on screen, the honest way to get it
is a real `/relay/2`, which is M5. §13 M4's PASS criteria are corrected accordingly.

### 7.3 Presence: extend `MATCH_STATE`, do not build a subsystem

`MATCH_STATE` is already the global snapshot broadcast to both visualizers. Add one optional field:

```jsonc
{ "type": "MATCH_STATE", "version": 1, "timestamp": 0, "phase": "IN_PROGRESS",
  "hp": { "1": 100, "2": 100 }, "winnerId": null, "reason": null,
  "connected": { "1": true, "2": false } }
```

`connected[p]` is **true iff `/relay/p` is currently registered** — i.e. that player's belt-to-engine
path is live. That is the thing the HUD needs to distinguish "P2 is playing badly" from "P2 is not
here". Visualizer presence is deliberately *not* surfaced: a phone knows whether it is connected, and
nothing in the demo needs to know about the other phone.

No new message, no heartbeat protocol, no timeout logic. `ConnectionRegistry` already holds the exact
truth; this exposes it.

### 7.4 Two documented-but-unimplemented behaviours (one is mandatory)

- **Mandatory: full-state snapshot on visualizer connect.** `app_messages.md` §2.5 already requires
  it and nothing implements it. Without it a phone shows placeholder text until an HP change — which,
  in the P1-only configuration, never comes. Fix: on `/visualizer/*` registration, send that
  connection the current `MATCH_STATE`. Also broadcast `MATCH_STATE` whenever any relay or visualizer
  connects or disconnects, so `connected` stays live.
- **Out of scope: POSITION echo.** §2.3 says positions are echoed; `engine/app.py` never does. Not
  needed for this demo, and implementing it would add a message flow the HUD does not use. Recorded as
  a known doc/code divergence, not scheduled.

### 7.5 `SENSOR_WINDOW.triggerId` — relay → Ultra96

```jsonc
{ "type": "SENSOR_WINDOW", ..., "stalePct": 0.0, "triggerId": 2 }
```

Optional, `0–7`, omitted entirely in continuous mode. Documented in `app_messages.md` §3.1 as a demo
input feature that a real classifier ignores. `0`/absent means "not a triggered capture".

### 7.6 Complete protocol diff

| File | Change | Breaking? |
|---|---|---|
| `shared/protocol/app_messages.md` | new §2.8 `ACTION_RECOGNIZED`; `MATCH_STATE.connected`; `SENSOR_WINDOW.triggerId`; note that §2.3 POSITION echo is unimplemented | no |
| `shared/schemas/action_recognized.schema.json` | new | no |
| `shared/schemas/match_state.schema.json` | optional `connected` | no |
| `shared/schemas/sensor_window.schema.json` | optional `triggerId` | no |
| `shared/protocol/cg_protocol.h` | document flag bit 2 as "frame is part of a triggered capture"; bits 5–7 as trigger id; add shift/mask macros | no — bit *values* and header layout unchanged, golden vectors unaffected |
| `shared/enums/enums.yaml` | **no change** (no new enum members) | — |
| `shared/protocol/VERSION` | **no change** — everything is additive | — |

The last two rows matter: no enum change means no `shared/generated/` regeneration, and no version
bump means no `UNKNOWN_VERSION` drops during a partial rollout across the three machines.

---

## 8. SINGLE-PLAYER OPERATION

### 8.1 What breaks today with only P1

Nothing crashes — and that is the problem. Every press produces `action_not_resolved` in the engine
log and **absolute silence on the wire**. A phone connected to `/visualizer/1` displays its
placeholder strings forever. The rig looks dead while every link is healthy. `log.txt` is a 49-second
recording of precisely this.

### 8.2 What makes it work

1. `ACTION_RECOGNIZED` is emitted before, and independently of, the resolver's verdict → the classified
   action reaches Unity with no opponent, no positions, and no combat.
2. `MATCH_STATE.connected` → the HUD shows `P2: Offline` instead of implying a broken two-player match.
3. `MATCH_STATE` on visualizer connect → the HUD is populated the instant the phone attaches.
4. The HUD renders `P2 HP: —` (not `100`) when `connected["2"]` is false, so nobody reads an absent
   player as a full-health one.

Engine-side state is untouched: `GameStateTask.__post_init__` still creates both `PlayerState`s at
`starting_hp`, and `MatchState.hp` still validates both keys. Presence is a *display* fact layered on
top; the state model does not learn about absence. That is the minimal change and it keeps P2 plug-in
trivial.

### 8.3 Supported modes after this plan

| Mode | Behaviour |
|---|---|
| **P1 only** | P1 actions display on P1's phone (and P2's, if one is attached). `outcome: IGNORED` (`STATE_ONLY` for guard), `targetId: null`, `P2: OFFLINE`. **This is a PASS.** |
| **P2 only** | Symmetric. Requires `relay.p2.yaml` + a second belt; no code difference |
| **P1 + P2** | Both recognitions broadcast; resolution additionally produces `COMBAT_EVENT` + `MATCH_STATE` + `COMBAT_FEEDBACK` once both positions are fresh |

The target display for P1-only is a **terminal feed**, exactly the brief's:

```
Engine: CONNECTED
P1: CONNECTED
P2: OFFLINE

> P1 weapon = SWORD          <- printed on the FIRST action after the weapon press, not on the press
> P1 used HORIZONTAL_SWING
> P1 used THRUST
> P1 used GUARD
```

The weapon line is derived from `ACTION_RECOGNIZED.weapon`, because `WEAPON_CHANGED` is relay→engine
only and is not broadcast to visualizers (§9.4). Later, with P2 present, the same feed grows lines like `> P2 CONNECTED`, `> P1 HIT P2`, `> P2 HP = 90`.
No fake P2 is required, or created, for any of it.

---

## 9. UNITY VISUALIZER — IMMEDIATE PATH

### 9.1 Verified current state (do not restart this work)

`visualizer_demo/CG4002VisualizerDemo_UnityTemp/My project/` is the live Unity **6000.6.2f1** project.
The older hand-assembled `visualizer_demo/CG4002VisualizerDemo/` tree has since been removed. Evidence that the project is real and already compiling:

- `unity_batch_createscene7.log`: `CreateDemoScene: saved 'Assets/Scenes/Demo.unity' via Unity's own
  scene serializer.`, **no `error CS` lines**, `Exiting batchmode successfully now!`, return code 0.
- `Assets/Scenes/Demo.unity` is 1628 lines of genuine Unity serialization with every expected object.
- `Packages/manifest.json` has `com.unity.nuget.newtonsoft-json 3.2.1` alongside URP 17.6.0,
  ugui 2.6.0, inputsystem 1.20.0.
- Scripts are complete and coherent: `Net/AppMessages.cs` (76 lines), `Net/EngineClient.cs` (203),
  `Controllers/DemoHudController.cs` (117), `Editor/CreateDemoScene.cs` (204).

**Compilation and scene generation are solved.** The remaining defects are all small and specific.

### 9.2 The blockers, in fix order

**B1 — every label renders nothing (null font).** `CreateDemoScene.CreateLabel` line 133:

```csharp
text.font = EditorGUIUtility.Load("LegacyRuntime.ttf") as Font;
```

`EditorGUIUtility.Load` resolves Editor-only resources, so the reference does not survive into the
saved scene: every `Text` in `Demo.unity` has `m_Font: {fileID: 0}`. Fix:
`Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf")`, then regenerate the scene. This is the
single highest-value fix — without it the HUD is blank even when every message arrives.

**B2 — `Demo.unity` is not in the build.** `ProjectSettings/EditorBuildSettings.asset` lists only
`Assets/Scenes/SampleScene.unity`. An APK would launch an empty URP sample scene. Fix: have
`DoCreate()` also set `EditorBuildSettings.scenes` to `Demo.unity` enabled, so regenerating the scene
and registering it can never drift apart again.

**B3 — TLS on Android will reject the engine's self-signed cert.** `EngineClient.Awake`:

```csharp
if (string.IsNullOrEmpty(PinnedCertSha256)) return;   // installs NO callback
```

A blank pin therefore means *default* validation, which fails on a self-signed cert — the opposite of
what the Inspector tooltip and `visualizer_demo/README.md` both claim ("Leave blank to accept any
cert"). The doc and the code disagree and the code is what runs. Two fixes, both wanted:
  - install the validation callback unconditionally; when `PinnedCertSha256` is blank, accept any cert
    and `Debug.LogWarning` loudly that validation is disabled;
  - add a `UseTls` bool that selects `ws://` vs `wss://`.

**The fallback is a TLS-terminating sidecar on the phone — not disabling engine TLS.** Do **not** set
`use_tls: false` in `engine.yaml`: the relay→engine TLS leg is already proven working and must stay
exactly as it is, and flipping the engine's listener would break that leg to fix a Unity problem.

Preferred path when IL2CPP will not accept the self-signed cert:

| Hop | What runs | Encryption |
|---|---|---|
| Unity → `ws://127.0.0.1:8080/visualizer/1` | `UseTls = false`, loopback inside the phone | none needed — never leaves the device |
| `socat` on the phone (Termux) | `socat TCP-LISTEN:8080,reuseaddr,fork OPENSSL:127.0.0.1:8443,verify=0` | TLS, to the tunnel's local end |
| the phone's existing SSH `-J` tunnel | unchanged: local 8443 → Ultra96 8443 | SSH |
| engine listener | unchanged: `use_tls: true`, self-signed cert | TLS |

`verify=0` is a demo-only concession, and it is made *inside* an SSH tunnel on a closed network — the
cert being skipped is one hop from the tunnel endpoint. This keeps engine and relay networking
unchanged while removing Unity/IL2CPP TLS from the critical path. If the unconditional callback *does*
work under IL2CPP, `wss://` direct is still the default and socat is never started.

**B4 — no reconnect.** `Connect()` calls `ConnectAsync` once; on failure it reports
`connect_failed: …` and stops forever. On a phone that is also fighting an SSH tunnel, one transient
failure ends the demo. Fix: a coroutine that retries with capped backoff (1, 2, 4, 8 s) whenever
`IsConnected` is false, and reconnects after the `null` disconnect sentinel.

**B5 — nothing displays an action.** `DemoHudController` subscribes to `OnCombatEvent`,
`OnMatchState`, `OnWeaponChanged`, `OnConnectionStatus` only. `HandleWeaponChanged` is dead code today
(the engine never broadcasts `WEAPON_CHANGED`, and this plan does **not** change that — the weapon is
read from `ACTION_RECOGNIZED.weapon` instead, §9.4). There is no action label at all — the primary
communications proof has nowhere to appear. Fix: add `ActionRecognized` to `AppMessages.cs`, a
dispatch case, and P1/P2 action + presence labels.

**B6 — player-settings defaults.** `companyName: DefaultCompany`, `productName: My project`,
**empty `applicationIdentifier`**, `AndroidTargetSdkVersion: 0`. An empty application identifier fails
an Android build — that is the actual blocker. Set a real company/product name and
`com.cg4002.visualizerdemo`.

**Do not force Target SDK 34.** `AndroidTargetSdkVersion: 0` means *automatic (highest installed)*,
which is the right setting for a sideloaded demo APK: it builds against whatever Android SDK platform
this Unity install actually has. Pinning 34 fails the build outright if platform 34 is not installed,
and there is no Play Store submission here to require a specific level. Leave it automatic unless a
build error names the target SDK. `AndroidMinSdkVersion 26` is already fine.

Not a blocker: there is no `EventSystem` in the scene. Nothing in the HUD is interactive, so none is
needed.

### 9.3 Fastest route to a visible HUD

The HUD can be brought up **before any engine change lands**, which is why it is Milestone 1: fix
B1/B2/B6, regenerate the scene, build, and confirm the labels render and the connection status line
reads something. Unity is a **terminal/debug visualizer** for this demo — no animations, no gameplay
graphics, no AR, no visual representation of any action. The existing capsule avatars, HP sliders and
`FlashAvatar` may stay because removing them is also work, but **no time is to be spent on visuals**. Then B3/B4 to get a stable connection, then B5 when `ACTION_RECOGNIZED` exists.
`cg-visualizer-sim` is the intermediate oracle throughout — it exercises the identical engine path, so
"the engine is fine, Unity is not" is always answerable without a phone.

### 9.4 HUD content — terminal/debug only

Three status lines plus an append-only text feed. That is the whole display.

```
Engine: CONNECTED
P1: CONNECTED
P2: OFFLINE

> P1 weapon = SWORD          <- emitted with the first action, NOT on the GPIO 4 press
> P1 used HORIZONTAL_SWING
> P1 used THRUST
> P1 used GUARD
```

With P2 connected, the same feed simply gains more lines — no new UI:

```
> P2 CONNECTED
> P1 used HORIZONTAL_SWING
> P1 HIT P2
> P2 HP = 90
```

Line rendering rules:

| Source | Line |
|---|---|
| `ACTION_RECOGNIZED`, `action != IDLE` | `> P{playerId} used {action}` |
| `ACTION_RECOGNIZED`, `action == IDLE` and `guardState == GUARDING` | `> P{playerId} used GUARD` |
| `ACTION_RECOGNIZED.weapon` differs from the last weapon this HUD printed for that player | `> P{playerId} weapon = {weapon}`, emitted **immediately before** that action's line |
| `COMBAT_EVENT` with a wire outcome | `> P{attackerId} {outcome} P{defenderId}` |
| `MATCH_STATE` hp change | `> P{id} HP = {hp}` |
| `MATCH_STATE.connected` transition | `> P{id} CONNECTED` / `> P{id} OFFLINE` |

`GUARD` is a **text line, nothing else** — no guard stance, no animation, no visual. Confidence,
`outcome` and `targetId` are useful for debugging and may be appended to the `[ACT]` line at the
implementer's discretion, but the demo-facing line is the short form above.

**Weapon-display timing — a real constraint, not a preference.** `WEAPON_CHANGED` is routed relay→engine
and applied to `PlayerState.weapon`, but **nothing forwards it to a visualizer** (§1.2), which is exactly
why `DemoHudController.HandleWeaponChanged` is dead code today. So:

- pressing GPIO 4 updates the relay's `current_weapon` and the engine's `PlayerState.weapon`, and
  **prints nothing on the phone**. That is correct behaviour, not a fault;
- Unity learns the confirmed weapon from the **next `ACTION_RECOGNIZED.weapon`**, and prints the weapon
  line just above that action's line;
- the HUD therefore keeps one `lastWeapon` string per player and prints the weapon line only on change
  (first action included, since the initial value is empty).

Forwarding `WEAPON_CHANGED` to visualizers would make the press print immediately, and it stays an
**optional later cleanup** (§17 FUTURE item 16) — deliberately outside NOW scope, because it means a new
engine→visualizer flow for a cosmetic gain. Do not promise the immediate-print behaviour anywhere in the
demo procedure until that cleanup is actually done.

Reuse the existing `Text` labels and the `AppendFeedLine` ring. The existing HP sliders and avatars may
remain; do not build new ones, do not add prefabs or art, do not add ARFoundation.
`SendSyntheticPosition` stays as-is — it is P1's only POSITION source and it is needed later for the
combat test, but nothing in the P1-only display depends on it.

---

## 10. POSITIONING — FUTURE READINESS

**No positioning is implemented by this plan.** No movement controls, no localisation, no coordinate
validation, no positioning UI.

What is preserved untouched:

- `POSITION` message, its schema, and `InboundPosition` routing on `/visualizer/*`.
- `PlayerState.position`, `.heading_deg`, `.position_ts` and the per-(player, `position`) clock anchor.
- `resolver.py`'s full geometry: `position_max_age_ms` staleness, `_range_between` vs the move's
  `range_m`, `_bearing_deg` + `guard_arc_deg: 120` for the guard arc, the `parry_window_ms: 150`
  window, `block_multiplier: 0.25`.
- `DemoHudController.SendSyntheticPosition` (every 0.5 s from the avatar transform).

**Critical property: `ACTION_RECOGNIZED` is emitted before the resolver's position check, so no
position — real, static, or fake — is required for P1's classified action to appear.**

For the M4 combat test only, static demo positions are acceptable, and there is a concrete gotcha:
`Demo.unity`'s avatars sit at `(0, 0.5, 0)` and `(1, 0.5, 0)`, and the HUD sends
`(transform.x, transform.z)` — so P1 reports `(0, 0)`. But
`comms/configs/scenarios/demo_visualizer_scenario.yaml` walks P2 out to `(2.0, 1.0)` and `(1.5, 1.5)`,
i.e. **2.24 m and 2.12 m from P1 — beyond SWORD's 1.5 m reach**, so those steps resolve as `MISS`, not
`HIT`. Maximum reach anywhere in `weapons.yaml` is 2.8 m (SPEAR `CHARGED_THRUST`).

So M4 needs a new static scenario, e.g. a single step at `{x: 1.0, y: 0.0, heading: 180.0}` repeated —
1.0 m away, inside every SWORD move's range, with P2 facing P1 so guard/block behaviour is reachable
on purpose rather than by accident. Add it as a new file; do not edit the walking scenario.

---

## 11. FUTURE P2 SYMMETRY

What P2 will need, given this plan:

| Item | Status after P1 work |
|---|---|
| Second FireBeetle with `CG_DEMO_PLAYER_ID 2`, its own BLE name | config-only re-flash |
| `comms/configs/relay.p2.yaml` (BLE address, `player_id: 2`, same `trigger_mode: true`) | copy of P1's file |
| Second `cg-relay` process on Qian Hui's laptop | no code change |
| `/relay/2` + `/visualizer/2` | **already implemented** in `RELAY_PATHS`/`VISUALIZER_PATHS` |
| Second phone at `/visualizer/2` | same APK, `PlayerId = 2` in the Inspector |
| Second trigger map | the same file; the sim is per-window stateless in trigger mode |

Design choices made specifically to keep this true:

- **The Ultra96 sim's trigger mode is stateless per request.** The current `itertools.cycle` is
  per-connection state that would *interleave* two players' relays into one shared cycle — P2 would
  steal P1's next action. Trigger mode removes that whole failure class.
- **`PlayerWindower` stays one-instance-per-relay-process.** Trigger/capture state is per instance, so
  two relays cannot interfere.
- **`ACTION_RECOGNIZED` carries `playerId` and broadcasts to both visualizers.** No P1-shaped fields.
- **Presence is a map keyed `"1"`/`"2"`**, not a boolean pair of special cases.
- **`trigger_mode` is per-relay config**, so P2 can run continuous windowing while P1 runs triggered,
  if ever useful for a comparison.

Things that would make P2 hard, and are therefore excluded: any per-player state in the Ultra96 sim;
any hardcoded `playerId: 1`; a new message that names players positionally instead of by id; presence
tracked as "is the opponent there" rather than as a map.

---

## 12. DEPENDENCY-ORDERED IMPLEMENTATION TASKS

Ordered so that each group is independently testable. Groups **U (Unity)** and **F/R/E** can proceed
in parallel; within a group, order matters.

### 12.1 Unity (start here — Priority 1, no dependency on any other group)

**U1 — `Assets/Editor/CreateDemoScene.cs`**
- Function: `CreateLabel` (line 117) and `DoCreate` (line 25).
- Change: replace `EditorGUIUtility.Load("LegacyRuntime.ttf") as Font` with
  `Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf")`. At the end of `DoCreate`, set
  `EditorBuildSettings.scenes` to a single enabled `Assets/Scenes/Demo.unity` entry. Add the §9.4
  status labels (`EngineStatusLabel`, `Player1PresenceLabel`, `Player2PresenceLabel`) and wire them,
  plus the existing `CombatFeedText` (the terminal feed), to the HUD fields. No other new objects.
- Do NOT change: avatar positions, canvas scaler reference resolution, the HP slider construction
  (0–100 matches `starting_hp: 100`), or the `EngineClient` defaults `Host 127.0.0.1` / `Port 8443` /
  `PlayerId 1` — the phone reaches the engine through its own tunnel, so `127.0.0.1` is correct.
  Keep `BearerToken`/`PinnedCertSha256` blank in tracked source.
- Test: run the batch command (§13 M1); log must show the scene saved with no `error CS`; then confirm
  `Demo.unity` has no `m_Font: {fileID: 0}` and `EditorBuildSettings.asset` lists `Demo.unity`.

**U2 — `Assets/Scripts/Net/AppMessages.cs`**
- Change: add `public class ActionRecognized : Envelope` with `playerId`, `weapon`, `action`,
  `actionConfidence`, **`guardState`**, **`guardConfidence`**, `outcome`, `targetId` (`int?` — nullable,
  because `null` is the normal P1-only value). Add
  `[JsonProperty("connected")] public Dictionary<string,bool> Connected;` to `MatchState`.
- Do NOT change: existing `JsonProperty` names (they are the wire contract), or `AppMessage.PeekType`.
- Test: compiles; a hand-pasted JSON string deserializes in a scratch Editor menu item.

**U3 — `Assets/Scripts/Net/EngineClient.cs`**
- Change: (a) add `public bool UseTls = true;` and `public int Port = 8443;` stays — build the URI as
  `ws`/`wss` accordingly, so the socat fallback (§9.2 B3) is a two-field Inspector change
  (`UseTls = false`, `Port = 8080`) with **no engine or relay config change**;
  (b) in `Awake`, always install the `ServerCertificateValidationCallback`, accepting any cert with a
  `Debug.LogWarning` when `PinnedCertSha256` is blank; (c) add
  `event Action<ActionRecognized> OnActionRecognized` and a `case "ACTION_RECOGNIZED":` in
  `DispatchMessage`; (d) add a reconnect coroutine with capped backoff, triggered on connect failure
  and on the `null` inbox sentinel.
- Do NOT change: the `ConcurrentQueue` + drain-in-`Update` threading model (Unity API calls must stay
  on the main thread), the bearer header, the `/visualizer/{PlayerId}` path, or `SendPosition`.
- Test: `cg-visualizer-sim` first proves the engine; then the phone shows `engine: connected`, and
  killing/restarting the engine shows `disconnected` then `connected` again without an app restart.

**U4 — `Assets/Scripts/Controllers/DemoHudController.cs`**
- Change: subscribe to `OnActionRecognized`; render the §9.4 terminal feed and the three status
  lines; keep a per-player `lastWeapon` string and print `> P{id} weapon = {weapon}` from
  `ActionRecognized.weapon` whenever it changes, immediately before that action's line (**the GPIO 4
  press itself prints nothing — `WEAPON_CHANGED` is not broadcast to visualizers**); a guard report
  (`action == IDLE && guardState == GUARDING`) renders `> P1 used GUARD` — text only, no stance, no
  animation; show `P2: OFFLINE` when `MatchState.Connected["2"]` is false; raise
  `MaxFeedLines` to ~14 so the feed reads like a terminal.
- Do NOT change: `SendSyntheticPosition` (needed for M4) or the `FlashAvatar` coroutine (leave it
  wired — removing it is pointless work, and no new visual effects are to be added). Leave
  `HandleWeaponChanged` and the `OnWeaponChanged` subscription in place, still dead — they cost nothing
  and become live for free if the optional forwarding cleanup ever lands.
- Test: M2 PASS criteria.

**U5 — `ProjectSettings/ProjectSettings.asset`** (via Editor **API**, not hand-edited YAML, not the GUI)
- Change: `companyName`, a real `productName`, `applicationIdentifier` = `com.cg4002.visualizerdemo`.
- **How:** extend the existing `Assets/Editor/CreateDemoScene.cs` automation rather than clicking through
  Project Settings, so a fresh clone is correct after one batch-mode run and the settings cannot drift
  from the scene. All three have Editor APIs:
  `PlayerSettings.companyName`, `PlayerSettings.productName`, and
  `PlayerSettings.SetApplicationIdentifier(NamedBuildTarget.Android, "com.cg4002.visualizerdemo")`
  (the older `PlayerSettings.applicationIdentifier` setter also works). Follow the writes with
  `AssetDatabase.SaveAssets()` so batch mode persists them. Guard them so they are idempotent.
- Only fall back to the GUI if an API call turns out not to exist on Unity 6000.6.2f1 — and say so
  explicitly rather than hand-editing `ProjectSettings.asset`.
- Do NOT change: `AndroidTargetSdkVersion` — leave it `0` (**automatic / highest installed**). Do not
  force 34 unless the installed Unity Android SDK actually has platform 34 and a build error demands
  it. Also do not change `AndroidMinSdkVersion 26`, the scripting backend, or `activeInputHandler`.
- Test: run the §13 M1 batch command, then confirm `ProjectSettings.asset` shows the new
  `companyName`/`productName`/`applicationIdentifier` **without anyone having opened the GUI**; then an
  Android build completes and installs. If the build fails naming the target SDK, *then* set it to a
  platform that is installed.

### 12.2 Firmware

**F1 — `shared/protocol/cg_protocol.h`**
- Change: document bit 2 as "this frame belongs to a triggered capture"; redefine bits 5–7 as the
  3-bit trigger id; add `CG_FLAG_TRIGGER_ID_SHIFT 5` and `CG_FLAG_TRIGGER_ID_MASK (0x7u << 5)`.
- Do NOT change: packet-type values, header layout, nonce derivation, bits 0/1/3/4, or any payload
  struct. Golden vectors must remain valid untouched.
- Test: `pio test -e native_test` (CRC + nonce vectors) still passes.

**F2 — `comms/firmware/include/cg_config.h`**
- Change: add `CG_DEMO_ACTION_BUTTON_COUNT 3`, `CG_DEMO_ACTION_BUTTON_PINS {25, 26, 27}`,
  `CG_DEMO_ACTION_BUTTON_TRIGGER_IDS {1, 2, 3}`, `CG_DEMO_BUTTON_DEBOUNCE_MS 200`,
  `CG_DEMO_CAPTURE_FRAMES 40`. Comment that `CG_DEMO_CAPTURE_FRAMES` must equal the relay's
  `windowing.window_samples`.
- Do NOT change: `CG_DEMO_WEAPON_BUTTON_PIN 4`, the UUIDs, `CG_DEMO_PLAYER_ID`,
  `CG_DEMO_SENSOR_FRAME_INTERVAL_MS 20`, `CG_DEMO_HEARTBEAT_INTERVAL_MS 1000`.
- Test: compiles.

**F3 — `comms/firmware/src/main.cpp`**
- Functions: `setup()` (add `pinMode(..., INPUT_PULLUP)` per action pin), `loop()` (replace the single
  button poll with a debounced loop over all buttons), `send_sensor_frame()` (compute `flags` from the
  capture state instead of hardcoding 0).
- Change: on a debounced falling edge of an action button, if no capture is active, set
  `g_capture_remaining = CG_DEMO_CAPTURE_FRAMES` and `g_capture_trigger_id = id`. In
  `send_sensor_frame()`, if `g_capture_remaining > 0`, set
  `flags = CG_FLAG_MOTION_ACTIVE | ((g_capture_trigger_id & 0x7) << CG_FLAG_TRIGGER_ID_SHIFT)` and
  decrement. Apply the same debounce to the weapon button. Add one `Serial.printf` per capture start.
- Do NOT change: the handshake, `send_packet`, crypto, sequence counters,
  `drain_incoming_non_blocking()` (COMBAT_FEEDBACK must keep being ACKed), the 50 Hz cadence, the
  synthetic IMU waveform, or `send_rfid_event_next_weapon()`'s weapon cycling.
- Test: `pio run -e esp32dev -t upload` then `pio device monitor`; each press prints exactly one
  capture line, and holding a button prints one, not many.

### 12.3 Relay

**R1 — `comms/src/cg_comms/protocol/constants.py`**
- Change: add `FLAG_TRIGGER_ID_SHIFT: int = 5` and `FLAG_TRIGGER_ID_MASK: int = 0b111 << 5`.
  `FLAG_MOTION_ACTIVE` already exists.
- Do NOT change: `PacketType` values, `RELIABLE_PACKET_TYPES`, the LSB scaling constants.
- Test: import-only.

**R2 — `comms/src/cg_comms/messages/models.py`**
- Change: add to `SensorWindow`:
  `trigger_id: int | None = Field(alias="triggerId", default=None, ge=0, le=7)`.
- Do NOT change: any other field of `SensorWindow`, `ActionResult`, or `extra="forbid"`.
- Test: `pytest comms/tests/` — a window without `triggerId` must still validate.

**R3 — `comms/src/cg_comms/relay/windowing.py`**
- Class/function: `PlayerWindower`, `ingest_frame`.
- Change: add field `trigger_mode: bool = False` plus private `_armed`, `_capture_remaining`,
  `_capture_trigger_id`, `_refractory`. When `trigger_mode` is False, the existing stride path is
  unchanged. When True, implement §3.2's arm/count/emit/refractory logic, set `trigger_id` on the
  emitted window, and log a distinct event when a triggered capture is suppressed by the
  staleness/span gates (so a swallowed press is never silent).
- Do NOT change: `_scale_sample`, the stale/span thresholds, `_RequestIdCounter` wraparound,
  `on_weapon_confirmed`/`current_weapon`, or the weapon gate (no windows before a weapon is confirmed).
- Test: existing `comms/tests/test_windowing.py` must pass unmodified (that is the regression proof for
  continuous mode). Add: one tagged burst of 40 frames → exactly one window with the right
  `trigger_id`; 200 untagged frames → zero windows; two bursts → exactly two windows; a burst with no
  weapon confirmed → zero windows.

**R4 — `comms/src/cg_comms/config.py`**
- Change: `WindowingConfig.trigger_mode: bool = False`.
- Do NOT change: existing defaults (they are asserted against `shared/config/sensors.yaml` by
  `test_config_defaults_match_shared_sensors_yaml_and_plan`).
- Test: that test still passes.

**R5 — `comms/src/cg_comms/relay/app.py`**
- Function: the `PlayerWindower(...)` construction in `run_relay`.
- Change: pass `trigger_mode=settings.windowing.trigger_mode`. Update the `--console-level` help text,
  which currently tells the operator that `window_ready`/`action_classified` "fire several times a
  second at 50 Hz" — false in trigger mode.
- Do NOT change: `on_action_result` (it correctly returns early when no weapon is confirmed, and
  correctly forwards IDLE), the reconnect supervisor, or the backoff table.
- Test: M2.

**R6 — `comms/configs/relay.p1.yaml`**
- Change: add `trigger_mode: true` under `windowing`.
- Do NOT change: `window_samples: 40` (must equal `CG_DEMO_CAPTURE_FRAMES`), `stride_frames: 5` (still
  the continuous-mode value), the quality thresholds, `min_confidence: 0.6`,
  `request_timeout_ms: 120`, `max_in_flight: 2`, the omitted `bearer_token` (the env var must keep
  winning — pydantic-settings does not deep-merge), or the BLE address.
- Test: `cg-relay` boot log shows `trigger_mode=True`.

### 12.4 Ultra96 simulator

**S1 — `comms/configs/scenarios/demo_trigger_map.yaml`** (new) — the §5.2 table.

**S2 — `comms/src/cg_comms/mock/ultra96_sim.py`**
- Function: `_handle_client`, `main`.
- Change: add `--trigger-map`; when given, load the table and classify from
  `(parsed.weapon, parsed.trigger_id)` with the `default` fallback; when absent, keep the
  `itertools.cycle` path byte-for-byte. Extend the `ultra96_sim_classified` log with `trigger_id` and
  the matched row.
- Do NOT change: the line-delimited JSON framing, the `requestId` echo, the `ActionResult` shape, the
  `MessageDropped`/unexpected-type warnings, or the listener's `--host`/`--port` defaults.
- Test: with the sim running, send one crafted `SENSOR_WINDOW` with `"triggerId": 2` and confirm the
  reply is that row's action; send one with no `triggerId` and confirm `default`.

### 12.5 Engine + protocol

**E1 — `shared/protocol/app_messages.md`** — new §2.8 `ACTION_RECOGNIZED`; `MATCH_STATE.connected`;
`SENSOR_WINDOW.triggerId`; a note that §2.3's POSITION echo is specified but not implemented. Do NOT
change §2.4's statement that `COMBAT_EVENT` carries only the four wire outcomes.

**E2 — `shared/schemas/`** — new `action_recognized.schema.json`; optional `connected` in
`match_state.schema.json`; optional `triggerId` in `sensor_window.schema.json`. Do NOT touch
`combat_event.schema.json`.

**E3 — `comms/src/cg_comms/messages/models.py`** — `class ActionRecognized(_Envelope)` with
`player_id`, `weapon: RealWeapon`, `action: ActionClass` (**must permit `IDLE`** — a guard report is
`IDLE` + `GUARDING`, so do not narrow this to `CombatAction`), `action_confidence`,
**`guard_state: GuardState`**, **`guard_confidence: Confidence`**,
`outcome: CombatOutcome` (the full enum, not `WireCombatOutcome`), `target_id: PlayerId | None = None`;
add `MatchState.connected: dict[str, bool] | None = None`. The guard fields use the same aliases as
`Action`/`ActionResult` (`guardState`, `guardConfidence`), so they copy straight across. Do NOT relax
`MatchState`'s hp validator or widen `CombatEvent.outcome`.

**E4 — `comms/src/cg_comms/messages/validate.py`** — add `"ACTION_RECOGNIZED": ActionRecognized` to
`_TYPE_TO_MODEL` and the import list. Do NOT change the version gate or the drop-reason enum.

**E5 — `comms/src/cg_comms/engine/game_state_task.py`**
- Change: add an `on_action_recognized: Callable[[ActionRecognized], Awaitable[None]]` field and a
  `presence: Callable[[], dict[str, bool]]` provider. In `_resolve_one`, immediately after
  `decision = resolve(...)` and the `attacker.last_action` update, emit one `ActionRecognized` —
  before the IGNORED/STATE_ONLY/ON_COOLDOWN early return, so it fires for every recognised action.
- **Emit rule (corrected):** emit when
  `ev.action is not ActionClass.IDLE or ev.guard_state is not GuardState.IDLE`. Emit **nothing** only
  for pure idle (both IDLE). A rule of `action != IDLE` alone would silently drop every guard press,
  because guard arrives as `action: IDLE` + `guardState: GUARDING`.
- **Guard fields must reach `_resolve_one`.** `ActionEvent` (`resolver.py:18-23`) currently carries only
  `player_id`, `action`, `confidence`, `timestamp` — no guard — and it is what the `ReorderBuffer`
  hands back. Extend it with **defaulted** fields:
  `guard_state: GuardState = GuardState.IDLE` and `guard_confidence: float = 0.0`, populated at the one
  construction site, `_absorb` (`game_state_task.py:119-124`), from `event.action.guard_state` /
  `event.action.guard_confidence` — the same two fields `_apply_guard_state` already reads three lines
  later. Defaults are required: `resolve()` must stay free to
  ignore them, and `comms/tests/test_resolver.py`'s 4-kwarg `make_event` must keep passing
  **unmodified**. (`PlayerState` stores `guard_state` but no guard *confidence*, which is why the
  confidence rides on the event rather than being read back from state.)
- **`targetId` from presence, never from the resolver:**
  `target_id = d if (d := decision.defender_id) is not None and self.presence().get(str(d), False) else None`.
  `decision.defender_id` is `2` even with no P2 at all (`_other_player_id` over the always-constructed
  `PlayerState`s), so copying it would make Unity claim an absent target. See §7.2.
- In `_broadcast_match_state`, add `connected=self.presence()`.
- Do NOT change: the single-writer discipline, `_anchor` clock logic, the `ReorderBuffer` push/drain,
  the `_logged_post_match` latch, `_apply_guard_state`'s existing behaviour, `resolver.py`'s logic (it
  must stay pure — the only change there is the two defaulted `ActionEvent` fields, which `resolve()`
  does not read), the existing `action_not_resolved` log, or the `COMBAT_EVENT`/`COMBAT_FEEDBACK`
  emission conditions.
- Test: M2 PASS via `cg-visualizer-sim`, **including one guard press** producing exactly one
  `ACTION_RECOGNIZED` with `action: IDLE`, `guardState: GUARDING`, `outcome: STATE_ONLY`.

**E6 — `comms/src/cg_comms/engine/ws_server.py`**
- Change: expose the occupied relay paths (e.g.
  `ConnectionRegistry.relay_presence() -> dict[str, bool]`), and add optional
  `on_connect`/`on_disconnect` async hooks to `run_ws_server`'s handler, called after `register` and
  after `unregister` with the path.
- Do NOT change: the bearer check ordering (auth before path routing), `RELAY_PATHS`/`VISUALIZER_PATHS`,
  the duplicate-path replacement behaviour, `DropOldestQueue` depth handling, or the existing
  `ws_client_connected`/`ws_client_disconnected` log lines.
- Test: connect two clients, confirm presence reflects both.

**E7 — `comms/src/cg_comms/engine/app.py`**
- Change: add `on_action_recognized` → `broadcast_to_visualizers`; pass
  `presence=registry.relay_presence` into `GameStateTask`; on `/visualizer/*` connect, send **that
  connection** the current `MATCH_STATE` snapshot; on any connect/disconnect, broadcast `MATCH_STATE`
  so `connected` stays live.
- Do NOT change: `await task.start_match()` at startup (phase must be `IN_PROGRESS` immediately),
  the TLS context construction, or the `asyncio.gather` of server + task.
- Test: a `cg-visualizer-sim` that attaches *after* the engine has been running logs a `MATCH_STATE`
  immediately.

### 12.6 Config + docs

**C1 — `comms/DEMO_RUNBOOK.md`** — rewrite §2 "What a healthy run looks like" for trigger mode (one
`window_ready` + one `action_classified` + one `ACTION_RECOGNIZED` **per press**, not 10/s); add the
§15 failure rows; add the button wiring table.

**C2 — `comms/firmware/README.md`** — button wiring table, the `CG_DEMO_CAPTURE_FRAMES` ↔
`window_samples` coupling, and the debounce constant.

**C3 — `comms/configs/scenarios/demo_scenario.yaml`** — one header line noting it is the mock-rig
driver, no longer the demo's classifier source. Do NOT delete it (tests use it).

**C4 — new static-position scenario for M4** (§10). Do NOT edit `demo_visualizer_scenario.yaml`.

**C5 — two discrepancies to verify with the team, not silently "fix":**
- `comms/configs/relay.p1.yaml` has `tunnel_ssh_jump_host: stfjump.comp.nus.edu.sg` (matching
  `COMMS_IMPLEMENTATION_PLAN.md` §8), but the `log.txt` command block records working manual tunnels
  through **`stujump.comp.nus.edu.sg`**. Since `tunnel_enabled: false` and the team opens tunnels by
  hand, nothing is broken today — but the config would fail if anyone ever set `tunnel_enabled: true`.
  Confirm which host is correct before changing either.
- The `log.txt` Ultra96 command reads `--port8888` (missing space). Harmless typo in a pasted note;
  the runbook's version should be the canonical one.

**C6 — security item, flagged not scheduled:** `comms/firmware/include/cg_config.h` carries a concrete
32-hex PSK in tracked source, while its own header comment says "Never commit a real PSK — this file
ships with a placeholder and should stay that way in shared history." Either rotate the PSK and keep
the placeholder discipline, or accept it explicitly as a throwaway demo key. This is the user's call;
do not change the PSK unilaterally, because it must stay in sync with `comms/.env` / `relay.p1.yaml`
on another machine.

---

## 13. MILESTONES

Machine roles throughout: **DEV PC** (this checkout — editing, Git, Unity, APK build; no runtime
processes), **LAPTOP** (Qian Hui's — `cg-relay`, BLE, SSH forwarding), **ULTRA96**
(`cg-ultra96-sim` + `cg-engine`), **PHONE** (Termux tunnel + Unity APK). All three runtime machines
`git pull`; no divergent local edits.

### M1 — Unity visualizer on screen (Priority 1)

**Tasks:** U1, U2, U5 (U3/U4 land in M2). Terminal feed only — no visual work in this milestone or
any later one.

**Processes:** DEV PC only for the scene rebuild and APK; then ULTRA96 `cg-engine` and PHONE for the
connection check.

**Commands (DEV PC, PowerShell):**
```powershell
& "C:\Program Files\Unity\Hub\Editor\6000.6.2f1\Editor\Unity.exe" -batchmode -quit `
  -projectPath "C:\Users\T0332075\Desktop\CG4002\visualizer_demo\CG4002VisualizerDemo_UnityTemp\My project" `
  -executeMethod Cg4002.VisualizerDemo.EditorTools.CreateDemoScene.DoCreate `
  -logFile "C:\Users\T0332075\Desktop\CG4002\visualizer_demo\unity_batch_m1.log"
```
Then build the APK from the Editor (File > Build Settings > Android > Build) and
`adb install -r <apk>`.

**Expected log:** `CreateDemoScene: saved 'Assets/Scenes/Demo.unity' via Unity's own scene
serializer.`, no `error CS`, `Exiting batchmode successfully now!`.

**Expected Unity output:** the app opens on the HUD scene (not the URP sample scene) with **readable
text**; the status line reads `engine: connect_failed: …` or `engine: connected`.

**PASS:** legible labels on the phone, from `Demo.unity`, in a build — and the batch run alone has set
`companyName`, `productName` and `applicationIdentifier`, with no GUI step.

**Failure interpretation:** blank screen with correct-looking objects → font fix did not take (B1);
grey/blue empty scene → `Demo.unity` not in build settings (B2); build refuses to start → empty
`applicationIdentifier` (B6); a build error naming the Android target SDK → a target SDK was pinned to
a platform that is not installed, set it back to automatic (B6); `error CS` → U2's new class does not
compile.

### M2 — P1-only action pipeline (Priority 2 + 3)

**Tasks:** all of F, R, S, E + U3, U4. **This is the milestone that proves the demo.**

**Processes:** FireBeetle flashed; LAPTOP tunnel + `cg-relay`; ULTRA96 `cg-ultra96-sim` + `cg-engine`;
PHONE tunnel + APK. **No P2 anything. No visualizer_sim faking P2.**

**Commands:**
```bash
# ULTRA96 (two shells)
.venv/bin/cg-ultra96-sim --port 8888 --trigger-map configs/scenarios/demo_trigger_map.yaml
.venv/bin/cg-engine --config configs/engine.yaml

# LAPTOP (two shells)
ssh -J <user>@<jumphost> -L 127.0.0.1:8888:127.0.0.1:8888 -L 127.0.0.1:8443:127.0.0.1:8443 xilinx@<board> -N
.venv\Scripts\cg-relay.exe --config configs\relay.p1.yaml --no-tunnel

# PHONE (Termux), then launch the APK
ssh -J <user>@<jumphost> -L 127.0.0.1:8443:127.0.0.1:8443 xilinx@<board> -N

# PHONE, second Termux session — ONLY if Unity/IL2CPP rejects the self-signed cert (§9.2 B3).
# Engine and relay TLS stay untouched; Unity then uses ws://127.0.0.1:8080 instead of wss://:8443.
socat TCP-LISTEN:8080,reuseaddr,fork OPENSSL:127.0.0.1:8443,verify=0
```

**Procedure:** press the **weapon** button once. **Expect nothing on the phone yet** — `WEAPON_CHANGED`
is not broadcast to visualizers; confirm SWORD in the *relay* log (`weapon_confirmed`). Then press
**action button 1** once: the phone prints the weapon line and the action line together. Wait ~2 s.
Press it again. Then press button 2 (thrust), then button 3 (**guard**).

**Expected logs, per single action press:**
- relay: exactly one `window_ready` with a `trigger_id`, then exactly one `action_classified`
- ultra96-sim: exactly one `ultra96_sim_classified` naming the matched trigger row
- engine: exactly one `action_not_resolved outcome=IGNORED` **and** the new `ACTION_RECOGNIZED`
  broadcast

**Expected Unity output:**
```
Engine: CONNECTED
P1: CONNECTED
P2: OFFLINE

> P1 weapon = SWORD          <- appears with the first action, NOT at the weapon press
> P1 used HORIZONTAL_SWING
> P1 used HORIZONTAL_SWING
> P1 used THRUST
> P1 used GUARD
```

**PASS (all six):** (a) no action appears without a press; (b) one press → exactly one action line on
the phone; (c) button 2 shows `THRUST`; (d) **button 3 shows `> P1 used GUARD`** — this is the test of
the corrected emit rule; under the rejected `action != IDLE` rule this line would be missing while every
log upstream looked perfect; (e) the weapon press alone prints **nothing** on the phone, and the weapon
line appears with the first action instead — that is the designed behaviour while `WEAPON_CHANGED` is not
forwarded; (f) `P2: OFFLINE`, and nothing looks broken.

**Failure interpretation:** see §15 — this milestone is what the decoder is for.

### M3 — Repeatability ×3 (Priority 4)

**Processes:** unchanged from M2.

**Procedure:** press action button 1 three times with ~2 s gaps. Then hold it down for 3 s. Then press
buttons 1, 2, 3 in sequence.

**PASS:**
- exactly **3** `ACTION_RECOGNIZED` for three presses — not 2, not 4, not 30;
- **holding** the button produces exactly **one** action (proof the capture is atomic and the
  refractory state works);
- the sequence shows the three distinct lines in order, ending with `> P1 used GUARD`;
- during a 30 s idle stretch with no presses: **zero** `window_ready`, **zero** `ACTION_RECOGNIZED`,
  while `HEARTBEAT` keeps the link alive.

**Failure interpretation:** more actions than presses → §15 row 5; fewer → §15 row 2 (span/staleness
gate rejecting the capture, look for the `window_suppressed` log); any action during idle → the relay
is still in continuous mode (`trigger_mode` not read, or `relay.p1.yaml` not pulled on the laptop).

### M4 — Temporary P2 combat test (Priority 5)

**Purpose:** prove combat/HP once, then put it away. **Nothing in M2/M3 may depend on this.**

**Extra process:** on the LAPTOP, `cg-visualizer-sim --player-id 2` driven by the new C4 static
scenario (P2 at 1.0 m facing P1), attached to `/visualizer/2`. P1's real phone must also be sending
POSITION (`SendSyntheticPosition` is on, so it does, reporting `(0, 0)`).

**PASS:** a button-1 press yields `ACTION_RECOGNIZED outcome=HIT` **and** a `COMBAT_EVENT` with
`damage: 10` **and** a `MATCH_STATE` showing `hp["2"] == 90`; the phone's feed reads
`> P1 HIT P2` then `> P2 HP = 90`; `P2: OFFLINE` stays **OFFLINE** (a visualizer is not a relay —
that is the designed meaning of presence).

**Expect `targetId: null` here, and that is a PASS.** Presence gates `targetId` on `/relay/2`, and this
test has no `/relay/2` — only a visualizer. Combat still resolves because the *resolver* uses
`PlayerState`, which exists for both players regardless. `targetId: 2` requires a real second relay,
i.e. M5. Do not "fix" this by reading `decision.defender_id` directly (§7.2).

**Failure interpretation:** `outcome=IGNORED` with both positions flowing → position staleness
(`position_max_age_ms: 1000`; check the engine's per-channel clock anchor, and that the sim is sending
every ≤1 s); `outcome=MISS` → the static position is out of range, re-read §10; `ON_COOLDOWN` on a
rapid second press → expected, SWORD HORIZONTAL_SWING has an 800 ms cooldown.

**Then stop the sim.** M2 and M3 must be re-runnable and still PASS with it gone.

### M5 — Real P2 (PLAN ONLY, do not implement)

Copy `relay.p1.yaml` → `relay.p2.yaml` (BLE address, `player_id: 2`); flash a second FireBeetle with
`CG_DEMO_PLAYER_ID 2` and a distinct BLE name; run a second `cg-relay` on the laptop; install the same
APK on a second phone with `PlayerId = 2`. No engine, protocol, or sim change. Expected: both phones
show both players' actions; presence reads `1: true, 2: true`; attacks resolve once both positions
flow.

### M6 — Real positioning (PLAN ONLY, do not implement)

Replace `SendSyntheticPosition` with a real source feeding the same `POSITION` message. Everything
downstream — `position_max_age_ms`, range vs `range_m`, `guard_arc_deg`, parry window — already exists
and is untouched by this plan. No protocol change is expected.

---

## 14. DEMO-DAY PROCEDURE

**T-30 min — bring up, in this order (each step's failure is unambiguous only if the previous
succeeded):**

1. **ULTRA96:** `cg-ultra96-sim --port 8888 --trigger-map configs/scenarios/demo_trigger_map.yaml`,
   then `cg-engine --config configs/engine.yaml`. Confirm `engine_listening` and the sim's
   `ultra96_sim_listening`.
2. **LAPTOP:** open the SSH tunnel (8888 + 8443). Confirm it stays up.
3. **LAPTOP:** `cg-relay --config configs\relay.p1.yaml --no-tunnel`. Confirm the boot log shows
   `trigger_mode=True`, then `ble_connected`, `handshake_complete`, `timesync_seeded`, and `heartbeat`
   traffic. **No `window_ready` should appear.** Silence here is correct.
4. **PHONE:** open the Termux tunnel (8443), then launch the app. Confirm `Engine: CONNECTED` on the
   HUD and `ws_client_connected path=/visualizer/1` in the engine log. If the app cannot complete the
   TLS handshake, start the socat sidecar in a second Termux session
   (`socat TCP-LISTEN:8080,reuseaddr,fork OPENSSL:127.0.0.1:8443,verify=0`) and switch the app to
   `UseTls = false`, `Port = 8080`. **Do not touch `engine.yaml`** — the relay→engine TLS leg is proven
   and stays exactly as it is.
5. **Sanity press:** weapon button once → `weapon_confirmed` **in the relay log**, and deliberately
   **nothing on the phone** (`WEAPON_CHANGED` is not forwarded to visualizers — §9.4). Then action
   button 1 once → the phone prints `> P1 weapon = SWORD` followed by one `> P1 used …` line.
   **Then stop touching it** — leftover actions during the introduction are the thing that made the old
   demo look broken.

**Narration order (this is also the safest order if something fails):**

1. "Silence is correct." Show the idle rig: heartbeats, no windows, no actions. This is the fix.
2. "One press, one action." Press button 1. Point at the sequence: firmware tags 40 frames → relay
   windows once → **Ultra96 decides** → engine → phone. Say plainly that the phone's label came from a
   decision made on the Ultra96, not from the belt.
3. "Different inputs, different actions." Button 2 → `> P1 used THRUST`. Button 3 →
   `> P1 used GUARD` — a *defensive* classification, carried as a separate protocol field
   (`guardState`) alongside the action, and reported by the same one-press→one-classification path.
4. "Recognition is not combat." Point at `-> IGNORED / no target`: the action arrived and was
   recognised; there is no opponent, so the game did nothing with it. **This is the single most
   important point in the demo** and it is also the honest answer to "why is nothing taking damage".
5. "Weapon changes are not gestures." Weapon button (the phone stays quiet — weapon state lives in the
   relay and engine, and is reported with the next action, not pushed on the press); then press
   button 1 → `> P1 weapon = SPEAR` and the Ultra96 returns `SWING`, the SPEAR's move. Same button,
   different weapon, different action, decided upstream.
6. "P2 plugs in." Show `P2: Offline` and say what M5 needs: a config file, a flash, a process, a phone.
7. *(Optional, only if M4 was rehearsed and time allows)* start the P2 position sim and show a HIT with
   HP dropping. Note out loud that `targetId` is still `null`: a visualizer is not a relay, so the
   system declines to claim a connected opponent even while combat resolves.
8. **"Where the decision is made" — say these five things, in these words** (this is the question that
   decides whether the demo is believed; the full wording is §3.3):
   - the FireBeetle sends **IMU samples plus trigger metadata** — a flag bit and a 3-bit trigger id;
   - it never sends an `ActionClass`, so **the FireBeetle does not classify**;
   - `cg-ultra96-sim` maps `(weapon, triggerId)` to an action **deterministically, from a table**;
   - **the simulator is NOT inferring the gesture from the synthetic IMU waveform** — say this out
     loud, unprompted, because it is what an examiner will assume otherwise;
   - the real Ultra96 classifier later classifies **from the IMU samples**, over the identical
     `SENSOR_WINDOW` → `ACTION_RESULT` interface, and is free to ignore `triggerId` completely.

**Fallbacks, in order of preference:**

- Phone TLS fails → start the socat sidecar on the phone and set `UseTls = false`, `Port = 8080`
  (§9.2 B3). Engine and relay TLS are **unchanged**; the plaintext hop is loopback inside the phone.
  Say it out loud: the SSH tunnel and the engine's own TLS are both still there.
- Phone unusable → run `cg-visualizer-sim` on the laptop and narrate its `visualizer_sim_received`
  lines. The engine path is identical; only the display differs.
- FireBeetle unusable → `cg-firebeetle-sim`, but note honestly that it drives the *continuous* path;
  the trigger story then has to be told from the code and the test output, not live.
- Ultra96 unreachable → `use_mock: true` + a local engine on the laptop. Say clearly that classifier
  and engine are then co-located with the relay, which is not the deployed topology.

**Do not, on the day:** change `window_samples` or `CG_DEMO_CAPTURE_FRAMES` (they must match), edit
YAML on a runtime machine instead of pulling, or restart the engine to "fix" a stuck match — there is
no reset path, and a restart is the *intended* way to start a new match (`match_over_actions_ignored`
says so).

---

## 15. FAILURE DECODER

| # | Symptom | Most likely cause | How to confirm | Fix |
|---|---|---|---|---|
| 1 | **Button press, no FireBeetle event** | Wrong pin, no `INPUT_PULLUP`, button not wired to GND, or firmware not reflashed | `pio device monitor` shows no capture line; `digitalRead` stays HIGH | Re-check wiring (press = LOW = to GND), confirm `CG_DEMO_ACTION_BUTTON_PINS`, reflash. Also check the relay is past `handshake_complete` — `loop()` returns early before the CCCD subscription and handshake, so **no button works until the relay has subscribed** |
| 2 | **Firmware event, no sensor window** | (a) no weapon confirmed yet — the weapon gate blocks every window; (b) the span/staleness gate rejected the capture; (c) `trigger_mode` not enabled on the laptop's config | (a) relay has no `weapon_confirmed`; (b) the new `window_suppressed` log names the failing gate; (c) relay boot log shows `trigger_mode=False` | (a) press the weapon button first — **this is the most common demo mistake**; (b) re-press (a transient BLE stall), and check `stale_pct`; (c) `git pull` on the laptop and confirm `relay.p1.yaml` has `trigger_mode: true` |
| 3 | **Window, no classifier response** | Sim not running, tunnel down, or the 120 ms timeout expired (no retry by design) | Relay logs `ultra96_request_timeout` or a connection error; the sim logs nothing | Restart the tunnel, then the sim. If the tunnel is fine but timeouts persist, the round trip exceeds 120 ms — that is a network problem, not a code one; do not raise the timeout mid-demo |
| 3a | **Weapon press prints nothing on the phone** | **Correct behaviour, not a bug.** `WEAPON_CHANGED` is relay→engine only; nothing forwards it to a visualizer. Unity reads the weapon from `ACTION_RECOGNIZED.weapon` | Relay logs `weapon_confirmed`; the phone's feed is unchanged until the next action, which prints `> P1 weapon = …` then `> P1 used …` | Nothing. If the weapon line never appears *even with an action*, U4's `lastWeapon` comparison is missing. Immediate-on-press printing needs the optional `WEAPON_CHANGED` forwarding cleanup (§17 FUTURE 16), which is out of NOW scope |
| 4a | **Every button works except guard** | The emit rule was implemented as `action != IDLE`. Guard is `action: IDLE` + `guardState: GUARDING`, so it is the one press that classifies correctly and then emits nothing | Relay logs `action_classified` and the engine logs `action_not_resolved outcome=STATE_ONLY`, but no `ACTION_RECOGNIZED` follows | Use the corrected rule: emit when `action != IDLE` **OR** `guardState != IDLE` (§7.2, E5) |
| 4b | **Unity says the action had a target, but no P2 exists** | `decision.defender_id` was copied into `targetId`. It is `2` even with no P2, because both `PlayerState`s always exist | `ACTION_RECOGNIZED targetId=2` while `MATCH_STATE.connected["2"] == false` and no `/relay/2` is connected | Gate `targetId` on relay presence (§7.2, E5) |
| 4 | **Classifier responds, Unity shows nothing** | Isolate by layer: relay-side confidence gate → engine-side drop → visualizer not connected → Unity dispatch | Relay: `action_below_confidence_gate` (result < `min_confidence: 0.6`)? Engine: `inbound_message_dropped` / `inbound_unexpected_type` (the new message not registered in `_TYPE_TO_MODEL`)? `ws_client_connected path=/visualizer/1` present? Unity console: `unhandled message type 'ACTION_RECOGNIZED'` (U2/U3 not built into the APK) or nothing at all (font bug B1 — the text is there, invisibly) | Fix the layer the evidence names. Rebuild the APK if Unity logs `unhandled`. If Unity logs nothing at all and the engine shows the broadcast, suspect B1/B2 before suspecting the network |
| 5 | **One press produces multiple actions** | (a) `trigger_mode` is off → continuous 10 Hz windowing; (b) no firmware debounce → two edges per press; (c) the refractory reset is missing → the capture re-arms while still tagged | (a) `window_ready` every ~100 ms; (b) two capture lines on the serial monitor for one press; (c) back-to-back windows exactly `window_samples` apart | (a) enable `trigger_mode`; (b) raise `CG_DEMO_BUTTON_DEBOUNCE_MS`; (c) fix the refractory logic in R3 — require one untagged frame before re-arming |
| 6 | **P1-only action works but no damage** | **This is correct behaviour, not a bug.** No opponent → `resolve()` returns `IGNORED` at the position check → no `COMBAT_EVENT` | `ACTION_RECOGNIZED outcome=IGNORED targetId=null`; engine `action_not_resolved outcome=IGNORED` | Nothing. Say so during the demo. Combat needs M4 or M5 |
| 7 | **P2 absent** | Expected | `MATCH_STATE.connected == {"1": true, "2": false}`; no `/relay/2` connection | Nothing. `P2: Offline`, `P2 HP: —`. If the HUD instead shows `P2 HP: 100`, U4's presence handling is missing — the engine always carries both HP keys by design |
| 8 | **A combat action becomes IGNORED** | Walk `resolver.py`'s ladder in order: no attacker → phase not `IN_PROGRESS` → IDLE (`STATE_ONLY`) → no weapon → invalid move for this weapon → below `min_confidence` → cooldown (`ON_COOLDOWN`) → missing/stale position | `action_not_resolved` logs `outcome`, `action`, `confidence`, `weapon`. `IGNORED` with a valid action and weapon almost always means **stale or missing POSITION** | Most common: no POSITION (P1-only — correct). Second: wrong weapon for the action (press the weapon button, or use the trigger row that matches the held weapon). Third: match already ended — `match_over_actions_ignored` appears once; restart the engine |
| 9 | **Connection failure** | Which link? | Relay→engine: `session_failed` + `reconnecting` (the supervisor retries forever). Phone→engine: `engine: connect_failed: …` on the HUD. Relay→BLE: `ble_connect_failed`. Relay→Ultra96: connection refused | Tunnels first (they are the most common failure and the least visible). TLS: a cert error on the phone is B3 — fall back to `UseTls = false` inside the tunnel. Bearer mismatch closes the WS immediately at the handshake: check `CG_RELAY_ENGINE_LINK__BEARER_TOKEN` on the laptop and the Inspector field on the phone against the engine's configured token. **A phone TLS failure is fixed on the phone** (socat + `UseTls = false`), never by disabling `use_tls` in `engine.yaml` — that would break the working relay→engine leg |

**Two cross-cutting rules for reading any failure:**

- **Check the weapon gate first.** `PlayerWindower` emits nothing before `on_weapon_confirmed`, and the
  relay's `on_action_result` also returns early when `current_weapon is None`. A rig that looks
  completely dead is usually just missing the first weapon press.
- **Silence in the relay log is now normal.** Under `trigger_mode`, no `window_ready` between presses
  is the *designed* state. The old runbook's "several per second" expectation is obsolete (§16).

---

## 16. PREVIOUS-PLAN ITEMS NOW OBSOLETE

Stated explicitly so none of these is silently carried forward.

| Source | Obsolete recommendation | Replacement |
|---|---|---|
| `docs/COMMS_IMPLEMENTATION_PLAN.md` §7 | "`strideFrames: 5` (emit every 100 ms → 10 Hz, 87.5% overlap)… when `counter % strideFrames == 0` emit a `SENSOR_WINDOW` if the buffer is full" — as the **demo's** behaviour | Still correct as the *real-game* design and still the code default. **Not** the demo's behaviour: `trigger_mode: true` makes emission trigger-driven. Both paths coexist; the config chooses |
| `docs/COMMS_IMPLEMENTATION_PLAN.md` §14 checkpoint 5c | "Windows emitted at 10 Hz" as a PASS criterion | In trigger mode the PASS criterion is **one window per press and zero when idle**. 10 Hz is now a *failure* signature for the demo (§15 row 5) |
| `comms/configs/scenarios/demo_scenario.yaml` as the demo classifier source | The 6-step `itertools.cycle` — the direct cause of automatic attacks | `demo_trigger_map.yaml`, keyed by `(weapon, triggerId)`. The scenario file stays for the mock rig and the e2e tests |
| `comms/DEMO_RUNBOOK.md` §2 | "A healthy run shows `window_ready` and `action_classified` several times a second" | One of each **per press**; silence between presses is healthy |
| `comms/DEMO_RUNBOOK.md` §8 | Point `EngineClient.Host` at the laptop's **LAN IP**, and regenerate certs with `scripts/gen_certs.sh ./certs 365 <laptop-LAN-IP>` because the SANs cover only 127.0.0.1/localhost | Obsolete since the phone's own SSH `-J` tunnel was proven: the phone forwards 8443 to `127.0.0.1`, so `Host = 127.0.0.1` is correct and the **existing** cert SANs already match. No cert regeneration, no LAN IP, no laptop relay hop |
| Implicit in the old plan | A P2 must exist (real or faked) for the pipeline to demonstrate anything | `ACTION_RECOGNIZED` is emitted independently of resolution. P1-only is a first-class PASS |
| Implicit in the old plan | `COMBAT_EVENT` / HP change is the proof that comms works | The proof is `ACTION_RECOGNIZED`. `COMBAT_EVENT` proves *combat*, which is a different claim (§6) |
| **This plan's own first draft** | Emit `ACTION_RECOGNIZED` only when `action != IDLE` | **Wrong — corrected.** Guard is `action: IDLE` + `guardState: GUARDING`, so that rule drops the guard button entirely. The rule is `action != IDLE` **OR** `guardState != IDLE`; only pure idle is silent (§7.2) |
| **This plan's own first draft** | `targetId` = `decision.defender_id` | **Wrong — corrected.** That field is `2` even with no P2. `targetId` is gated on `/relay/{id}` presence and is `null` otherwise (§7.2) |
| **This plan's own first draft** | TLS fallback = `use_tls: false` in `engine.yaml` | **Wrong — corrected.** Terminate TLS on the phone with socat and point Unity at `ws://127.0.0.1:8080`. The engine listener and the relay→engine leg are never reconfigured (§9.2 B3) |
| **This plan's own first draft** | Set `AndroidTargetSdkVersion 34` | **Wrong — corrected.** Leave it automatic (highest installed). Only the empty `applicationIdentifier` is a real blocker (§9.2 B6) |
| **This plan's own first draft** | "a trigger reaches the relay with zero wire changes" | **Imprecise — corrected.** Reusing reserved `flags` bits 5–7 is a backward-compatible **wire-semantics extension**: no new packet type, no size/layout change, no crypto change, no golden-vector churn — but the meaning of those bits is new (§1.5) |
| **This plan's own first draft** | A `P1 ACTION: … -> IGNORED / no target` status-panel HUD | Replaced by the terminal feed (`> P1 used HORIZONTAL_SWING`). Unity is a terminal/debug visualizer: no animations, no gameplay graphics, no AR, no visual action representation (§9.4) |
| Candidate button list in the brief | RELOAD / GRENADE / SHIELD buttons | No such `ActionClass` members or `weapons.yaml` moves exist (§4.2). Not added — that is a game-design change, not a comms demo |
| `shared/protocol/app_messages.md` §3.2 | "the relay parses ACTION_RESULT behind an adapter that tolerates both a single class and a probability vector, and ignores unknown keys" | Was untrue when written (`extra="forbid"`). **Since fixed:** `ActionResult` now uses `extra="ignore"`, and `action_result.schema.json` allows additional properties |

---

## 17. SONNET IMPLEMENTATION HANDOFF

Standing constraints for every task below: the FireBeetle must never name an action; the Ultra96 side
owns classification; no test may be weakened to pass; the three runtime machines `git pull` (never
hand-edit source there); `shared/enums/enums.yaml` and `shared/protocol/VERSION` are not to be touched.

**Six corrections that override the first draft of this plan.** They are cheap to get right now and
expensive to discover on demo day:

| # | Correction |
|---|---|
| 1 | **Emit `ACTION_RECOGNIZED` when `action != IDLE` OR `guardState != IDLE`.** Guard is `action: IDLE` + `guardState: GUARDING`; the narrower rule drops the guard button while every upstream log looks perfect |
| 2 | **`ACTION_RECOGNIZED` carries `guardState` + `guardConfidence`** alongside `action`/`actionConfidence`. Unity renders a guard as the text line `> P1 used GUARD` — no stance, no animation |
| 3 | **`targetId` comes from relay presence, never from `decision.defender_id`** (which is `2` even with no P2). `null` whenever `/relay/<id>` is not registered |
| 4 | **Unity is a terminal/debug visualizer.** Status lines + an append-only text feed. No animations, gameplay graphics, AR, or visual action representation. Existing HP bars/avatars may stay; spend no time on visuals |
| 5 | **TLS fallback is socat on the phone**, not `use_tls: false` in `engine.yaml`. The relay→engine TLS leg is proven and stays untouched |
| 6 | **Do not pin `AndroidTargetSdkVersion`.** Leave it automatic (highest installed). Only the empty `applicationIdentifier` is a real build blocker |
| 7 | **The GPIO 4 weapon press prints nothing on the phone.** `WEAPON_CHANGED` is relay→engine only and is **not** forwarded to visualizers in NOW scope. Unity reads the weapon from `ACTION_RECOGNIZED.weapon` and prints `> P1 weapon = SWORD` immediately before the **first action** after the change |
| 8 | **Set the player settings through Editor APIs, not the GUI and not hand-edited YAML.** Extend the existing `CreateDemoScene` automation |

Unchanged from the accepted plan: Design C triggers, one press → one classification, the
`ACTION_RECOGNIZED` concept, P1-only as a first-class PASS, P2 symmetry by config, positioning deferred,
Unity first.

### NOW — get the phone showing a classified action

1. **U1** `Assets/Editor/CreateDemoScene.cs` — `Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf")`
   instead of `EditorGUIUtility.Load` (line 133); register `Demo.unity` in `EditorBuildSettings.scenes`
   at the end of `DoCreate`; add the three §9.4 status labels and keep `CombatFeedText` as the feed.
   No new visual objects.
2. **U5** player settings — **through the Editor API in that same automation, not the GUI and not by
   hand-editing `ProjectSettings.asset`**: set `PlayerSettings.companyName`,
   `PlayerSettings.productName`, and
   `PlayerSettings.SetApplicationIdentifier(NamedBuildTarget.Android, "com.cg4002.visualizerdemo")`
   (the older `PlayerSettings.applicationIdentifier` setter is also fine), then
   `AssetDatabase.SaveAssets()` so batch mode persists them. Make the writes idempotent.
   **Leave `AndroidTargetSdkVersion` at `0` (automatic).** One batch-mode run must be enough for a fresh
   clone; only fall back to a GUI step if an API genuinely does not exist on Unity 6000.6.2f1, and say so
   rather than editing YAML. Regenerate the scene, build, install. **This is M1.**
3. **F1** `shared/protocol/cg_protocol.h` — document flag bit 2 as "triggered capture" and bits 5–7 as
   the trigger id; add `CG_FLAG_TRIGGER_ID_SHIFT`/`_MASK`. No value changes, so golden vectors stay
   valid. Describe this in commits and docs as a **backward-compatible wire-semantics extension**, not
   as "zero wire changes".
4. **F2/F3** firmware — action buttons on GPIO 25/26/27 with trigger ids 1/2/3 (**GPIO 27 is the guard
   button and stays a button**), 200 ms debounce applied to the GPIO 4 weapon button too, and a
   40-frame atomic capture that tags `flags`. Do not touch the handshake, crypto, cadence, or
   `drain_incoming_non_blocking()`.
5. **R1–R6** relay — flag shift/mask constants; optional `SensorWindow.triggerId`;
   `PlayerWindower.trigger_mode` with arm/count/emit/refractory (default `False` so every existing test
   passes unmodified); `WindowingConfig.trigger_mode`; pass it through `relay/app.py`;
   `trigger_mode: true` in `relay.p1.yaml`. No guard-specific relay work: `on_action_result`
   (`relay/app.py:103-117`) already copies `guard_state`/`guard_confidence` into `Action`, and
   `passes_confidence_gate` (`relay/ultra96.py:31-36`) already admits `IDLE`.
6. **S1/S2** Ultra96 sim — `demo_trigger_map.yaml` and `--trigger-map`, classifying from
   `(weapon, triggerId)`. The guard row is `action: IDLE` + `guard_state: GUARDING`; every other row
   must set `guard_state: IDLE` explicitly, because an omitted field defaults to `IDLE` and therefore
   *releases* a held guard. Keep the `--scenario` cycle intact for the tests.
7. **E1–E7** engine + protocol:
   - `ActionRecognized` model with `guardState`/`guardConfidence`, `action: ActionClass` (**must permit
     `IDLE`**), `targetId: PlayerId | None = None`; register it in `_TYPE_TO_MODEL`; schema;
     `app_messages.md` §2.8.
   - Add **defaulted** `guard_state` / `guard_confidence` to `ActionEvent` (`resolver.py:18-23`),
     populated in `_absorb` (`game_state_task.py:119-124`). Defaults are mandatory:
     `comms/tests/test_resolver.py`'s 4-kwarg `make_event` must keep passing unmodified, and `resolve()`
     must not read them.
   - Emit one `ACTION_RECOGNIZED` in `_resolve_one` **before** the IGNORED/STATE_ONLY/ON_COOLDOWN early
     return, under the rule `action != IDLE or guard_state != IDLE`.
   - `targetId` gated on `self.presence()`; `MATCH_STATE.connected` from `ConnectionRegistry`;
     `MATCH_STATE` snapshot on visualizer connect and on any connect/disconnect.
   - `resolver.py`'s logic stays pure and unchanged.
8. **U2/U3/U4** Unity — `ActionRecognized` (with the guard fields) + `MatchState.connected` in
   `AppMessages.cs`; `UseTls`, unconditional cert callback, reconnect backoff, and the
   `ACTION_RECOGNIZED` dispatch case in `EngineClient`; the §9.4 terminal feed in `DemoHudController`,
   including a per-player `lastWeapon` string so `> P{id} weapon = {weapon}` is printed from
   `ActionRecognized.weapon` on change, immediately before that action's line. **Do not add any
   engine→visualizer `WEAPON_CHANGED` flow** — leave `HandleWeaponChanged` in place and dead.
   **This completes M2.**

**Done when:** one press → exactly one action line; **the guard button produces `> P1 used GUARD`**;
**the weapon press alone prints nothing and the weapon line arrives with the first action**; zero lines
while idle; `P2: OFFLINE` with `targetId: null`; and the whole thing still works with no P2 of any kind.

### NEXT — prove it is repeatable and honest

9. **M3** — three presses → exactly three lines; a held button → exactly one; 30 s idle → zero.
10. **C1/C2/C3** docs — rewrite the runbook's "healthy run" section for trigger mode, document the
    button wiring (including the guard button) and the `CG_DEMO_CAPTURE_FRAMES` ↔ `window_samples`
    coupling, mark `demo_scenario.yaml` as the mock-rig driver, record the socat TLS fallback as a
    phone-side step that changes no server config, and state that **a weapon press is confirmed in the
    relay log, not on the phone** — the phone shows the weapon with the next action.
11. New tests: windowing (one burst → one window with the right `trigger_id`; untagged frames → none;
    two bursts → two; no weapon → none) and **one engine test that a guard `Action` produces exactly one
    `ACTION_RECOGNIZED` with `outcome: STATE_ONLY`** — that is the regression guard for correction 1.
    Existing `test_windowing.py` and `test_resolver.py` must pass **unmodified**.

### LATER — combat, only after NOW and NEXT pass

12. **C4** static P2 position scenario at ~1.0 m facing P1 (**not** the existing walking scenario — it
    walks out of SWORD range, §10).
13. **M4** — run `cg-visualizer-sim --player-id 2` briefly, confirm `HIT` / `damage: 10` /
    `> P2 HP = 90`, and confirm **`targetId` is still `null`** (a visualizer is not a relay — that is
    correct, do not "fix" it). Then stop it and re-verify M2/M3 still pass without it.

### FUTURE — plan only, do not implement

14. **M5 real P2** — `relay.p2.yaml`, a second flash with `CG_DEMO_PLAYER_ID 2`, a second relay process,
    a second phone with `PlayerId = 2`. This is also the first point at which `targetId` becomes `2`.
    No engine, protocol, or sim change should be needed; if one turns out to be needed, that is a defect
    in this plan's symmetry claims.
15. **M6 real positioning** — replace `SendSyntheticPosition` with a real source feeding the same
    `POSITION` message. All downstream geometry already exists.
16. Optional cleanups, each independent: POSITION echo (`app_messages.md` §2.3, unimplemented);
    `COOLDOWN_STATE` production (nothing emits it); **forwarding `WEAPON_CHANGED` to visualizers** — the
    only thing that would make the GPIO 4 press print on the phone immediately, and the reason
    `HandleWeaponChanged` is currently dead; explicitly **out of NOW scope**, because it adds a new
    engine→visualizer flow for a cosmetic gain that `ACTION_RECOGNIZED.weapon` already covers.
    Reconciling `app_messages.md` §3.2's "ignores unknown
    keys" claim with pydantic `extra="forbid"`; the `stfjump`/`stujump` jump-host discrepancy; and the
    committed PSK in `comms/firmware/include/cg_config.h`.
