# Online 1v1 Mode: Code Walkthrough

How the visualizer plays a real two-player match against the Python Game Engine.
It assumes you know the offline pipeline in [StateEngine.md](StateEngine.md), sections 1 and 3:
queues, `MessageDispatcher` and `GameState`.

Open in VS Code with `Cmd+K V` so the diagrams line up.

---

## 1. The big picture

Online mode keeps the visualizer's design: the phone decides nothing, and every change on screen
comes from a message. The one difference is who sits on the engine side of the two queues.
`OfflineEngine` is switched off, and `OnlineEngineClient` takes its place:

```
 ┌──────────────────────────── PHONE (Unity, OnlineScene) ────────────────────────────┐
 │                                                                                    │
 │  AR camera ─▶ PositionReporter ─┐                                                  │
 │  DummyPlacer (tap origin) ──────┼─▶ dispatcher.Outbound ─┐                         │
 │  DebugPanel (RESET) ────────────┘                        │                         │
 │                                                          ▼                         │
 │  SharedArenaCalibration ──IsReady──▶  OnlineEngineClient  (Net/)                   │
 │  (common origin + +Z line)           │  Update(): drain Outbound → JSON → outbox   │
 │                                      │  Update(): inbox → EngineWireCodec.Decode   │
 │                                      │  Run/Receive/Send: WebSocket on a thread    │
 │                                      │  OnGUI(): connection overlay                │
 │                                      ▼                                             │
 │  HUD, opponent avatar ◀─ GameState ◀─ MessageDispatcher ◀─ dispatcher.Inbound      │
 │                                                                                    │
 │  OfflineEngine: disabled                                                           │
 └──────────────────────────────────────┬─────────────────────────────────────────────┘
                     ws://127.0.0.1:8080/visualizer/{1|2}   (Bearer token)
                                        │
             Android: Termux socat :8080 ─TLS─▶ SSH tunnel ─▶ Ultra96 :8443
                                        │
 ┌────────────────────────────── ULTRA96 (Python, comms/) ────────────────────────────┐
 │  ws_server.py ── /visualizer/1, /visualizer/2, /relay/1, /relay/2, /control        │
 │       │                                                                            │
 │  game_state_task.py  (the one writer of match state)                               │
 │       ├─ presentation.py: start / pause / resume gating, builds STATE_SNAPSHOT     │
 │       └─ resolver.py:    HIT / MISS / BLOCK / PARRY from positions + guard         │
 │                                                                                    │
 │  Relays (one per player) ◀── BLE ── wearables (IMU, weapon selection)              │
 │  AI classifier (actions)                                                           │
 └────────────────────────────────────────────────────────────────────────────────────┘
```

| Direction | Message | When |
|---|---|---|
| Phone → engine | `POSITION {x, y, heading}` | 10×/s, only while calibrated |
| Phone → engine | `VISUALIZER_READY {ready}` | Whenever calibration changes (ready / not ready) |
| Engine → phone | `STATE_SNAPSHOT` | 10×/s: the complete state of both players |
| Engine → phone | `COMBAT_EVENT` | Once per resolved attack |

The phone **never sends attacks**. Actions come from the wearables through the relays and the
AI classifier, straight to the engine.

---

## 2. What's new, file by file

All Unity paths are under `visualizer/Assets/CG4002/`.

| File | Lines | Role |
|---|---|---|
| `Scripts/Runtime/Net/OnlineEngineClient.cs` | 345 | The comms client: WebSocket, both queues, readiness, the overlay |
| `Scripts/Runtime/Net/EngineWireCodec.cs` | 144 | JSON ⇄ the message classes in `State/Messages.cs`; coordinate conversion |
| `Scripts/Runtime/Net/SharedArenaCalibration.cs` | 68 | Puts both phones in one shared coordinate frame |
| `Scripts/Runtime/Net/PhoneSetupStatus.cs` | 57 | Reads a snapshot for the "setup readings" diagnostics |
| `Scripts/Editor/OnlineIntegrationSetup.cs` | | Menus **CG4002 → Integration → Create Online Scene** and **Add Weapon Select Screen** |
| `Scripts/Runtime/UI/WeaponSelectScreen.cs`, `Shaders/Hologram.shader` | | Weapon selection before calibration (section 3.3a) |
| `Scripts/Editor/EngineTokenBuildStep.cs` | 61 | Copies the engine token from `comms/.env.engine` into the build |
| `Resources/engine_token.txt` | | The token baked into the app (empty in git) |
| `Tests/PlayMode/EngineWireCodecTests.cs`, `OnlineEngineConnectionTests.cs`, `PhoneCalibrationTests.cs` | | Tests for the three runtime pieces |
| `Assets/Scenes/OnlineScene.unity` | | `SampleScene` plus an `OnlineEngine` GameObject; `OfflineEngine` disabled |

**Small edits to existing files:**

| File | Change | Why |
|---|---|---|
| `State/Messages.cs` | `MatchPhase.Paused`; `WeaponChangedMessage.selected/onlyIfChanged`; `CooldownMessage.totalSeconds/paused/actionName/entries`; `PositionMessage.monotonicTimestampMs` | The engine pauses matches, players may have no weapon yet, cooldowns are per action |
| `State/GameState.cs` | Stores the fields above; `ConfigureLocalPlayer(id)`; per-action `RemainingFor(action)`; cooldowns freeze while paused | Same |
| `Controllers/AvatarController.cs` | `ConfigurePlayer(id)`; on enable, catches up on weapon/guard/position already in `GameState`; hides the weapon until one is selected | The avatar is now the *other phone's* player, and state can arrive before the arena is placed |
| `UI/HudController.cs` | Shows "SELECT WEAPON" until a weapon is chosen | |
| `AR/DummyPlacer.cs` | `BlockPlacementInput` hook | Taps on the overlay must not place the arena |
| `AR/PositionReporter.cs` | Stamps each position with a monotonic clock | The engine orders positions against attacks |

---

## 3. Walkthrough: from launch to a hit

### 3.1 Launch: `Awake` (OnlineEngineClient.cs:65)

1. **Disables `OfflineEngine`.** Otherwise `LocalGameRules` would also read Outbound (StateEngine.md §2).
2. `LoadSavedSettings`:
   - The bearer token comes from `Resources/engine_token.txt`, written at build time by
     `EngineTokenBuildStep` from `comms/.env.engine`.
   - The player (P1/P2) is this phone's last choice, from `PlayerPrefs`.
   - The token itself is never saved in the scene or in PlayerPrefs.
3. `ConfigureIdentity`:
   - `gameState.ConfigureLocalPlayer(player)`, so the HUD's "self" is this phone's player;
   - `opponent.ConfigurePlayer(other)`, so the avatar draws the other player.

`OnlineEngineClient` runs at execution order **−200**, before the dispatcher (−100). Messages it
decodes in `Update` are applied by the dispatcher in the same frame.

### 3.2 Connect (OnlineEngineClient.cs:95)

The overlay's **Connect** button:
1. Validates the endpoint (`ws://` or `wss://`, host and port only).
2. Opens `endpoint + "/visualizer/" + player`, e.g. `ws://127.0.0.1:8080/visualizer/1`, with header
   `Authorization: Bearer <token>`.

The engine treats the path as the phone's identity. A second phone on the same path replaces the first.

### 3.3 The network threads: `Run` / `Receive` / `Send` (lines 143–232)

```
 background tasks                          main thread (Update)
 ────────────────                          ────────────────────
 Receive: socket → inbox (max 64) ───────▶ inbox → Decode → dispatcher.Inbound
 Send:    outbox → socket ◀─────────────── Outbound → Encode → outbox (max 32)
 Run:     connect, reconnect with backoff 1 s, 2 s, 4 s, 8 s
```

- **Two private queues** (`inbox`, `outbox`) hold raw JSON strings. Only the main thread touches
  Unity objects or `GameState`.
- **Generation counter.** Every connection attempt gets a new `generation`. Messages tagged with an
  older one are dropped, so a dead socket's leftovers can't leak into the new session.
- **Self-healing:**
  - If the inbox fills (64) or no snapshot arrives for 5 s, the socket is aborted.
  - The reconnect then fetches a fresh snapshot. Nothing has to be replayed, because snapshots
    carry the complete state.
- **Backgrounding** the app (`OnApplicationPause`) aborts the socket and invalidates calibration.

### 3.3a Weapon selection (UI/WeaponSelectScreen.cs)

The order on each phone is **connect → choose a weapon → calibrate → play**.

**Who decides.** The wearable hub picks the weapon, not the phone:
1. The hub compares the player's arm tilt with each weapon's stored ready stance.
2. If one stance is held for 3 s (`WEAPON_LOCK_HOLD_MS` in `hardware/combined/right_wrist_game.ino`),
   it locks that weapon until the hub reboots.
3. The engine's snapshot then reports the weapon. Until then it sends `weapon: null`, which
   becomes `selected = false`.

**What the screen does.** It opens only when the engine says "no weapon", so it stays closed
before the first snapshot and in offline play.

```
 snapshot: weapon null  ──▶  open: hologram dummy per offered weapon, ~2.5 m ahead, facing you,
                                   mirrored, in that weapon's ready stance (its idle clip)
                                   "COPY THIS POSE · Hold the pose for 3 seconds"
                                   HUD hidden, arena placement reset and held off
 snapshot: weapon SWORD ──▶  confirm: chosen dummy flashes, "SWORD & SHIELD SELECTED" (1.5 s)
                       ──▶  close: dummies removed, HUD back, tap-the-origin calibration begins
```

- **Mirrored.** The dummy's weapon hand appears on the same side as the player's, so the player
  copies it like a mirror.
- **Which weapons are offered:** `weapons` in the Inspector. It is only the sword for now. Adding
  spear and mace there shows them side by side, once their ready stances are animated.
- **Following the player.** The dummies stand on the lowest detected floor plane (or 1.4 m below
  the camera). If the player turns away for 1.5 s, they move back in front of the camera.
- **If the hub drops its lock** (reboot), the screen opens again and calibration resets.
- **Online overlay.** It shows "Choose your weapon first" in place of the calibration steps.
- **Afterwards,** while the opponent is still choosing, the screen shows "Waiting for your
  opponent to choose a weapon".
- **The look** comes from `Shaders/Hologram.shader`: a translucent fill brighter at the
  silhouette, plus an outline in the weapon's accent colour.
- **Offline test, no hardware:**
  1. In `SampleScene`, select **OfflineEngine** and tick *Require Weapon Selection* on `LocalGameRules`.
  2. Press Play.
  3. Press **1** to play the hub locking the sword.
- **Setup:** **CG4002 → Integration → Add Weapon Select Screen** adds the screen to both scenes;
  **Setup Game Scene** also builds it.

### 3.4 Shared arena calibration (SharedArenaCalibration.cs)

**The problem:** each phone's AR coordinates start wherever that phone booted. So "1 m in front of
me" on P1's phone and on P2's phone are different points in the room.

**The fix:** both phones agree on one physical origin and one direction.

```
                 +Z line (tape on the floor)
                        ▲
                        │      2. stand here (≥ 0.3 m out) and press
                        │         "Confirm shared +Z direction"
                        │
                        ●  1. tap this origin in AR (reuses DummyPlacer)
```

1. **Tap the origin.** This reuses `DummyPlacer`: the "arena" Transform lands on the marked spot.
2. **`ConfirmFromMarkedForwardLine`** (line 54):
   - takes the floor direction from the origin to the phone;
   - rotates the arena so its +Z points that way;
   - records the origin and rotation, and sets `calibrated = true`.
3. Since both phones did this on the same spot and line, their arenas now coincide. `PositionReporter`
   already reports in arena coordinates (StateEngine.md §6), so both phones' positions now share a frame.

**It un-calibrates itself** (`ValidateCalibration`, line 40) if any of these happen:
- AR tracking is lost;
- the arena moves by more than 1 cm or 0.5°, e.g. MOVE DUMMY, or ARKit relocalising;
- the app goes to the background.

Each change bumps `Revision`. The client then flushes every queued position, since they belong to
the old frame, and sends `VISUALIZER_READY false`.

### 3.5 Every frame: `Update` (OnlineEngineClient.cs:234)

1. **Inbound.** Up to 32 JSON messages per frame go through `EngineWireCodec.Decode` into
   `dispatcher.Inbound`. A `STATE_SNAPSHOT` is also saved as `lastSnapshot` for the diagnostics.
2. **Readiness.**
   - `ready = foreground && calibration.IsReady`.
   - When it changes, the client sends `VISUALIZER_READY`.
3. **Outbound.** Drains `dispatcher.Outbound`:
   - **`PositionMessage`** (this player, only while ready) → `POSITION` JSON.
   - **`StartMatchRequestMessage`** (placing the arena, or RESET) is **not forwarded**. It only
     re-applies the last snapshot to refresh the display. Online, only the operator resets matches,
     through the engine's `/control` path.
   - Everything else is dropped. `SwordHitDetector` is disabled with `OfflineEngine`, so no
     `ActionMessage` arrives anyway.

### 3.6 The codec (EngineWireCodec.cs)

**Coordinates.** The engine uses a 2-D floor frame. The phone uses Unity's 3-D frame:

| | Unity arena | Engine |
|---|---|---|
| Floor axes | `x`, `z` (y is up) | `x`, `y` |
| Heading 0° | +z | +x |
| Heading direction | clockwise (seen from above) | counter-clockwise |

The conversion is `engine.y = unity.z` and `heading = (90 − yaw) mod 360`. The same formula maps
back, so `UnityHeading` simply calls `CanonicalHeading`.

**`Decode`** (line 62):

| Engine message | Becomes |
|---|---|
| `STATE_SNAPSHOT` | For **each** player: `WeaponChanged` (`selected = weapon != null`, `onlyIfChanged` so the avatar doesn't re-equip 10×/s), `Health`, `Guard`, `Position` (if fresh), `Cooldown` (all per-action entries). Then `MatchState` |
| `COMBAT_EVENT` | `ActionMessage` (attack animation), then `CombatOutcomeMessage` (HP and hit/block FX) |
| `MATCH_STATE`, `WEAPON_CHANGED`, `ACTION_RECOGNIZED`, `COOLDOWN_STATE`, `POSITION` | Ignored: legacy messages already covered by snapshots |

Engine phases map as:
- `WAITING`/`ALIGNING` → `Waiting`
- `IN_PROGRESS` → `Running`
- `PAUSED` → `Paused`
- `ENDED` → `Ended`

The decoder is strict: unknown enums, a wrong version, a duplicate player or a non-finite number
throws. The overlay then shows "Invalid engine message", and that message is skipped.

### 3.7 Engine side: when does the match run? (comms/src/cg_comms/engine/presentation.py:21)

The engine only starts (or resumes) when **both** players have all of these:
- their phone sent `VISUALIZER_READY true` (calibrated);
- their relay is connected;
- a weapon selected on the wearable;
- a position less than 1 s old.

If any of these drops during a match:
- the phase becomes `PAUSED` with reason `NOT_READY`;
- guards are cleared and cooldowns freeze.

On resume, cooldowns are extended by the paused time, and attacks from before the pause are discarded.

### 3.8 Engine side: one attack (comms/src/cg_comms/engine/resolver.py:62)

```
 wearable IMU ─▶ relay ─▶ AI classifier ─▶ engine: ActionEvent(P1, HORIZONTAL_SWING)
                                                     │
 resolve(): running? valid move for the weapon? confidence ≥ 0.6? off cooldown?
            both positions known and < 1 s old?
            distance P1↔P2 > move range?  ──yes──▶ MISS
            P2 guarding AND facing P1 (within 120°)?
                 guard raised ≤ 150 ms before ─▶ PARRY (0 dmg, attacker staggered)
                 otherwise                     ─▶ BLOCK (25 % damage)
            else                               ─▶ HIT (full damage)
                                                     │
 COMBAT_EVENT ─▶ both phones ─▶ Decode ─▶ ActionMessage + CombatOutcomeMessage
                                       ─▶ opponent avatar swings, HP bar drops, FX
```

Ranges and damage live in `shared/config/weapons.yaml`. Examples:

| Weapon | Move | Range | Damage | Cooldown |
|---|---|---|---|---|
| Sword | horizontal swing | 1.5 m | 10 | 0.8 s |
| Spear | thrust | 2.5 m | 12 | 0.7 s |
| Mace | vertical swing | 1.3 m | 16 | 1.2 s |

Match tuning (HP, parry window, guard arc) lives in `shared/config/match.yaml`. This settles the
"hit vs. miss" open decision in StateEngine.md §7: **the engine checks range from both positions.**

### 3.9 What each phone draws

- **HUD:** this phone's player (`GameState.Self`) and the opponent's HP, the weapon or
  "SELECT WEAPON", the cooldown of the displayed action, and the match phase.
- **Opponent avatar:**
  - glides to the opponent's **real position** from the snapshot;
  - turns to their heading and holds their weapon;
  - swings on `COMBAT_EVENT`.

  Because both phones share the arena frame, the avatar is drawn where the other player actually
  stands in the room.
- **Your own player** is not drawn. You are the camera.

### 3.10 Which animation an action plays

`COMBAT_EVENT.action` → `ActionType` (codec) → the weapon's `WeaponDefinition.moves` → an
`AttackSlot` → the animator state of the same name → that weapon's clip.

| Engine action | Slot | Motion (same for every weapon, from its own stance) |
|---|---|---|
| `HORIZONTAL_SWING` | Horizontal | Cocked to the right, slash across to the left |
| `VERTICAL_SWING` | Vertical | Raised overhead, chop straight down |
| `DIAGONAL_SWING` | Diagonal | High right to low left (the horizontal arc tilted 40°) |
| `THRUST` | Thrust | Elbow chambered back, straight lunge forward |
| `SPIN_ATTACK` | Spin | Wind up to the right, a full turn left with the weapon out, end facing the opponent |
| `UPPERCUT` | Uppercut | Dip low with the weapon at knee height, rise and drive it up overhead |
| `CHARGED_ATTACK` (sword, mace), `CHARGED_THRUST` (spear) | Charged | Each weapon's own: glow, hold, heavy strike |
| `SWING` (spear) | Horizontal | |

- **Where the clips come from:** generated from key poses in `Scripts/Editor/AnimationLibrary.cs`.
  - The six classifier attacks are shared motions.
  - Each weapon authors its own stance, guard and charged attack.
  - The mace plays the shared motions 10 % slower.
- **After changing a pose:** run **CG4002 → Generate Models**. Then **CG4002 → Render Preview
  Sheets** writes a film strip of every attack per weapon (`Preview_<Weapon>_Attacks.png`).
- **The spin** uses `Pose.rootYaw`, which turns the whole body, arms and feet included. The clip
  ends at −360°, so the avatar finishes facing the opponent without spinning back.
- **Every weapon animates all six attacks,** because the AI classifies all six for every weapon
  (`ai/config.py`, `MOVE_CLASSES`). Which ones can *land* is the engine's `weapons.yaml`.
  Currently:
  - sword: horizontal, vertical, diagonal, thrust;
  - spear: thrust only;
  - mace: vertical and diagonal.

  Moves outside that list are resolved as IGNORED and never reach the phone.

---

## 4. The overlay (OnlineEngineClient.OnGUI, line 290)

A Unity IMGUI panel in the top-left corner, scaled to the safe area:

```
 ┌──────────────────────────────────────────┐      collapsed while the match runs:
 │ [Hide panel]                             │      ┌────────────────┐
 │ Online P1 · Connected                    │      │ P1 · Running   │
 │ Match: Running · Move cooldown: 0.4s     │      └────────────────┘
 │ ws://127.0.0.1:8080       (endpoint)     │
 │ ********                  (token)        │
 │ [Player 1] [Player 2]                    │
 │ [Connect]   or [Disconnect / settings]   │
 │ Tap the shared floor origin, then stand  │
 │ on the marked +Z line.                   │
 │ Calibration: Ready                       │
 │ [Confirm shared +Z direction]            │
 │ [Show setup readings]                    │
 │   snapshot age, phase / reason,          │
 │   P1: relay on, ready, weapon SPEAR, x/y │
 │   P2: ...                                │
 │   Separation: 1.84 m                     │
 └──────────────────────────────────────────┘
```

- It **collapses by itself** when the match starts. Tap the button to reopen it.
- `DummyPlacer.BlockPlacementInput = PointerOverPanel`, so taps on the panel don't move the arena.
- **Setup readings** answer "why isn't the match starting?": which player has no relay, no
  calibration, no weapon or no position, and how far apart the engine thinks the players are.

---

## 5. Running it

The full procedure is in [docs/INTEGRATION_RUNBOOK.md](../../docs/INTEGRATION_RUNBOOK.md). For the phone:

1. Open `Assets/Scenes/OnlineScene.unity`. It is the only scene enabled in the build. If it's missing,
   use **CG4002 → Integration → Create Online Scene**.
2. Build to the phone. The engine token is copied in automatically when `comms/.env.engine` exists.
3. Network route:
   - **Android:** Termux runs an SSH tunnel and `socat` on `127.0.0.1:8080`.
   - **iPhone:** there's no Termux, so iPhones need another reachable `ws://`/`wss://` endpoint.
     This isn't solved yet.
4. In the app:
   1. choose P1 or P2 (different on the two phones);
   2. tap **Connect**;
   3. copy the dummy's pose and hold it until the hub locks the weapon (3 s, two buzzes);
   4. tap the shared origin;
   5. stand on the +Z line and tap **Confirm**.
5. In the Unity editor, without AR, untick `requireARTracking` on `SharedArenaCalibration`.

---

## 6. Known gaps and notes

- **The offline scene still exists** (`SampleScene`), but it isn't in the build. `OfflineEngine`
  and `SwordHitDetector` only matter there.
- **No fixed spawn.** Players stand wherever they physically are, and range is real distance.
- **Status text is overwritten.** `Update` sets `Status` from the connection state every frame, so
  one-shot messages flash for a single frame, e.g. "Calibration confirmed" or "Match reset is
  controlled by the engine operator".
- **The wire format here supersedes the draft in StateEngine.md §6.** Differences: `version` instead
  of `schemaVersion`, upper-case enums, a 2-D `{x, y}` position, a counter-clockwise heading, and snapshots.
- **iPhone builds** have the same iOS project settings as before. Only the scene list changed.
- **Spin attack and uppercut don't reach the phone yet.** The visualizer animates and decodes them,
  but upstream they're still dropped. To enable them (comms and AI owners):
  1. `shared/enums/enums.yaml`: append `SPIN_ATTACK` and `UPPERCUT` to `ActionClass` (next free
     wire values), then regenerate `shared/generated`. Update the action enums in
     `shared/schemas/*.json`.
  2. `shared/config/weapons.yaml`: give each weapon that may use them a `damage`, `range_m` and
     `cooldown_ms`.
  3. `ai/classifier_server.py`: remove both from `COMMS_ACTION_MAP`, which currently reports them
     as `IDLE`, and update `comms/tests/test_ai_boundary.py::test_ai_only_classes_become_idle`.
