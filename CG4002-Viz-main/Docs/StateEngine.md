# State Engine and Integration

How game state reaches the visualizer, and how Comms and the Game Engine plug into it.
**Teammates integrating:** read sections 1, 2, 6 and 7.

Open in VS Code with `Cmd+K V` so the diagrams line up.

---

## 1. The idea

The visualizer decides nothing. Every change on screen starts as a **message** from the Game
Engine. Messages arrive through **one queue**, one **dispatcher** applies them once per frame to
**one state store** (`GameState`), and the HUD and avatar only listen to that store.

What the visualizer knows that the engine doesn't (where this player is standing, a request to
start a match) leaves through a **second queue**. Those two queues are the whole boundary:

```
          VISUALIZER                          │       ENGINE SIDE (exactly one enabled)
                                              │
 PositionReporter ─┐                          │  Offline: OfflineEngine GameObject
 DummyPlacer ──────┼─▶ dispatcher.Outbound ───┼─▶   LocalGameRules reads Outbound, decides
 DebugPanel ───────┘                          │     outcomes, answers on Inbound
                                              │     (+ MockDataInjector, SwordHitDetector)
                                              │
 HUD, Avatar ◀─ GameState ◀─ Dispatcher ◀─ dispatcher.Inbound ◀─┤  Online: comms client (to be written)
                                              │     Outbound → JSON → network → Game Engine
                                              │     Game Engine → network → JSON → Inbound
```

Swapping offline for online changes nothing on the left.

---

## 2. Integration: what to build

1. **Write a comms client** (any transport). It is a `MonoBehaviour` holding a reference to the
   `MessageDispatcher` (on the **Game** object). It has two jobs:
   - **Receive:** parse each JSON message (section 6) into the matching class in
     `State/Messages.cs`, then `dispatcher.Inbound.Enqueue(message)`. This is safe from the
     network thread.
   - **Send:** `while (dispatcher.Outbound.TryDequeue(out var m))`, serialize `m` and send it.
     This can run in `Update` or on the client's own thread, but it must be the **only** reader
     of Outbound.
2. **In the scene:** add the client to a GameObject and **disable `OfflineEngine`**. Otherwise
   `LocalGameRules` would also read Outbound and take messages meant for the network.
3. **The Game Engine's obligations:**
   - On a `START_MATCH_REQUEST`, send the full starting state for **both** players (`WEAPON_CHANGED`, `HEALTH`, `GUARD`), then `MATCH_STATE Running`.
   - `defenderHealth` is the **resulting** health, so one lost message can't leave it wrong for good.
   - After an attack, send `COOLDOWN` with the seconds until that player can attack again.
   - The visualizer never applies its own Outbound messages; state changes only when the engine says so.

Nothing else in the visualizer needs to change.

---

## 3. Inside the visualizer

```
 Inbound ──drain all──▶ MessageDispatcher ──(1) Apply──▶ GameState  (state lives here)
 (any thread)           (main thread, first     │   HealthChanged, WeaponChanged, GuardChanged,
                         Update each frame)     │   CooldownStarted, PositionChanged, PhaseChanged
                               │                ▼
                               └──(2) event──▶ ActionReceived, OutcomeReceived
                                                │
                              ┌─────────────────┴──────────────┐
                              ▼                                ▼
                       HudController (screen)       AvatarController (world)
                                                         └▶ AvatarView ─events─▶ CombatFxController
```

Three rules:
1. **Producers only know the queues.**
2. **Only the dispatcher writes to `GameState`**, by calling `Apply`.
3. **Consumers only listen.** They never compute outcomes.

**State vs. events.** State is things that *are* (health, weapon, guard, cooldown, position, match
phase). It is stored in `GameState`, and a late listener can just read it. Events are things that
*happen* (an attack, a hit). They are raised once and not stored. A hit is both: the dispatcher
applies the new health **first**, then raises `OutcomeReceived`, so the bar has dropped by the time the flash plays.

**One frame:**
- `MessageDispatcher` runs at execution order −100, before every other `Update`.
- It drains **all** waiting messages, so a burst doesn't become lag.
- Everything drawn that frame reflects that frame's messages.

---

## 4. Files

All under `Assets/CG4002/Scripts/Runtime/`.

| File | Role |
|---|---|
| `State/Messages.cs` | Every message class, grouped by direction |
| `State/MessageQueue.cs` | Thread-safe queue; `Inbound` and `Outbound` are both one of these |
| `State/MessageDispatcher.cs` | Owns the two queues; drains Inbound into `GameState`, raises events |
| `State/GameState.cs` | Latest state of both players and the match; a change event per field. Select **Game** in Play mode to watch it in the Inspector |
| `Controllers/AvatarController.cs` | The opponent avatar: weapon, guard, glide to position, attack animation, hit/block FX |
| `UI/HudController.cs` | Health bars, attack-ready bar, weapon, GUARDING, HIT / BLOCKED, match result |
| `UI/DebugPanel.cs` | RESET (sends `START_MATCH_REQUEST`), MOVE DUMMY, TRACKING |
| `AR/DummyPlacer.cs` | Tap-to-place the arena; sends `START_MATCH_REQUEST` once placed |
| `AR/PositionReporter.cs` | Sends this player's `POSITION` 10×/s once placed |
| `Offline/LocalGameRules.cs` | Stand-in Game Engine. Rule: refuse if not running or on cooldown (0.6 s); a guarding defender blocks; otherwise hit for 10 |
| `Offline/MockDataInjector.cs` | Fake opponent (guards, side-steps) and editor keys |
| `Offline/SwordHitDetector.cs` | Stand-in for the IMU: blade touches dummy → outbound `ACTION` (see PropTracking.md) |

`LocalGameRules` keeps **its own** copy of health and guard and never reads `GameState`. It plays
the server, and the real engine can't read the phone's memory either.

---

## 5. Example: my sword hits the dummy (offline)

```
1  SwordHitDetector: blade enters the dummy → Outbound: Action(P1, HorizontalSwing)
   (in the editor, press H, which calls rules.Attack directly)
2  LocalGameRules.Update reads it → Attack(P1): running ✓, off cooldown ✓, P2 guarding ✗ → HIT, 10
   Inbound: Action(P1) · CombatOutcome(P2, Hit, health 90) · Cooldown(P1, 0.6)
3  Next frame, MessageDispatcher:
     Action          → ActionReceived (P1 isn't the avatar, ignored)
     CombatOutcome   → GameState P2 health 90 → HealthChanged → HUD bar
                     → OutcomeReceived → AvatarController → fx.PlayHit
     Cooldown        → GameState → CooldownStarted → HUD attack-ready bar
```

Online, step 2 happens in the Game Engine, and the action in step 1 comes from the IMU instead.

---

## 6. Wire format (draft, to agree with Comms)

JSON, one object per message. Every message carries:

```json
{ "type": "COMBAT_OUTCOME", "schemaVersion": 1, "timestamp": 1791345600.123, "player": 2, ... }
```

- `type` picks the class.
- `schemaVersion` must be 1, or the visualizer drops the message with a warning.
- `timestamp` is in Unix seconds.
- `player` is 1 or 2 and means *who the message is about*.
- Field names match the C# fields. Enums are sent as their names (e.g. `"Hit"`).

**Engine → visualizer (Inbound)**

| `type` | Class | Fields | `player` is |
|---|---|---|---|
| `WEAPON_CHANGED` | `WeaponChangedMessage` | `weapon`: `SwordAndShield` / `Spear` / `Mace` | the player |
| `GUARD` | `GuardMessage` | `guarding`: bool | the player |
| `ACTION` | `ActionMessage` | `action`: an `ActionType` name (`Combat/CombatTypes.cs`) | the attacker |
| `COMBAT_OUTCOME` | `CombatOutcomeMessage` | `attacker`, `outcome` (`Hit`/`Miss`/`Block`/`Parry`), `damage`, `defenderHealth` | the defender |
| `HEALTH` | `HealthMessage` | `health`, `maxHealth` | the player |
| `COOLDOWN` | `CooldownMessage` | `seconds`: float | the player |
| `POSITION` | `PositionMessage` | `position`: `{x,y,z}` metres, `headingDegrees` | the player |
| `MATCH_STATE` | `MatchStateMessage` | `phase` (`Waiting`/`Running`/`Ended`), `winner` | unused |

**Visualizer → engine (Outbound)**

| `type` | Class | Fields | When |
|---|---|---|---|
| `POSITION` | `PositionMessage` | `position`, `headingDegrees` of this phone's camera | 10×/s once the arena is placed |
| `START_MATCH_REQUEST` | `StartMatchRequestMessage` | none | arena placed; RESET pressed |
| `ACTION` | `ActionMessage` | `action` | **offline only**, by SwordHitDetector |

**Arena coordinates** are metres, with y up. The origin is where the arena was placed, and +z
points from there towards where the camera was at placement. `headingDegrees` is clockwise from +z,
seen from above. Only the dummy model is scaled, never the arena, so the metres are real.

---

## 7. Open decisions (team)

| Question | Why it matters | Options |
|---|---|---|
| **How is hit vs. miss decided?** | Decides whether SwordHitDetector stays offline-only | Engine checks range from both players' `POSITION`s · visualizer reports blade contact (SwordHitDetector becomes an online reporter) · every classified action counts |
| **Shared arena origin** | Each phone's origin is where *it* placed the arena, so two phones' positions don't line up | Both phones place on the same printed floor marker (image tracking is already set up) · one fixed spot measured by hand |
| **Which phone is P1 / P2?** | `GameState.localPlayer` is set in the Inspector (default P1) | Fixed per build · assigned by the engine on connect (would need a new message) |
| **Who may start or reset a match?** | Each phone sends `START_MATCH_REQUEST` when it places the arena | Engine starts when both have asked · either player can reset |

---

## 8. Trying it

- **Editor, no phone:**
  1. Open `Assets/Scenes/SampleScene` and press Play (XR Simulation runs).
  2. Click a detected floor plane to place the dummy.
  3. Use the keys: **H** I hit the dummy · **G** toggle its guard · **C** it hits me · **R** restart.
  4. Select **Game** and watch `GameState` in the Inspector.
- **Phone:** tap the floor to place the dummy, then swing the prop into it. RESET, MOVE DUMMY and TRACKING are bottom-right.
- **Tests:** Window → General → Test Runner → PlayMode → Run All.
