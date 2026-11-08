# Demo Mode

A self-contained mode for demonstrating and testing the visualizer on **one phone**, with **no hardware
and no game engine**. Everything the wearables and the engine would do is driven by on-screen buttons
and resolved on the phone with the engine's own rules. Status: **built** (2026-11-07). Section 0 is how
to use it; the rest is the spec it was built to, with notes where the build differs.

Why `SampleScene` wasn't enough: on a phone it could only play the horizontal swing (by prop contact),
with one weapon fixed at build time, a flat 10 damage, no range or facing, and no control over the enemy.

## 0. Using it

1. In the editor, run **CG4002 → Integration → Refresh Player Views** once (it builds the demo controls
   into `OnlineScene`), then **Cmd+S**, then build as usual.
2. On the phone, in the online panel, tap **DEMO MODE (no hardware)**. No token, tunnel or engine needed.
3. Tap **CHOOSE** under your weapon, then under the enemy's.
4. Tap the floor to place the enemy.
5. Play with the bar at the bottom-left:
   - **YOU / ENEMY:** whose attacks and guard the bar plays.
   - **6 move icons:** attack (grey sweep = cooling down).
   - **GUARD:** hold it down to guard.
   - **CHANGE WEAPONS:** back to step 3.
   - RESET (bottom-right) restarts the match.
6. To leave demo mode, close the app.

What to try: walk back past the move's range (a MISS); hold GUARD as the enemy, then attack as YOU
(BLOCKED, 25% damage); tap GUARD just before the attack lands (PARRY, no damage); guard as YOU while
facing away from the enemy, then attack as ENEMY (it still hits).

Code: `Offline/DemoMode.cs` (switch-over and control bar), `Offline/LocalGameRules.cs` (the demo
engine), `Offline/GameRules.cs` (reads the YAML), `UI/WeaponSelectScreen.cs` (pick buttons),
`UI/HoldButton.cs`, `Editor/RulesBuildStep.cs`, `Editor/HudBuilder.BuildDemoControls`.

---

## 1. Architecture

Demo mode is another **engine side** behind the visualizer's two queues (StateEngine.md §1):

```
 on-screen buttons ─▶ dispatcher.Outbound ─▶ DemoEngine (on the phone) ─▶ dispatcher.Inbound ─▶ GameState ─▶ HUD, avatar, first person
 (attack, guard,                             rules from weapons.yaml +
  weapon choice)                             match.yaml, same as the engine
```

- **DemoEngine** replaces today's `LocalGameRules` (the offline stand-in) and is used by both demo mode
  and `SampleScene`. *Built:* `LocalGameRules` itself became the demo engine (same class name, so both
  scenes and the tests keep their references). It answers with the messages the real engine sends: weapon, health, guard, cooldowns
  (per move), match state, `ACTION_RECOGNIZED` (drives your first-person view) and `COMBAT_EVENT`
  (drives the enemy avatar and the outcome).
- Everything downstream is unchanged: HUD, cooldown icons, avatar animations, first-person view, FX,
  prop tracking.

## 2. Where it lives and how it starts

- A **DEMO MODE** button in the online connection panel of the normal build (`OnlineScene`). One build
  does both. (The button may move out of the panel later.)
- Tapping it: the online client stays disconnected and switches off; the DemoEngine switches on.
- `SampleScene` stays as the editor sandbox (keyboard keys), running the same DemoEngine.

## 3. Flow

1. **DEMO MODE** tapped.
2. **Weapon selection by buttons.** The selection screen's three hologram dummies (spear, sword & shield,
   mace) are shown with a **button under each**:
   - first **your** weapon,
   - then the **enemy's** weapon.

   This replaces the wearable hub's pose lock.
3. **Place the enemy** by tapping a detected floor spot (as today). No shared calibration: there's one
   phone. You can walk closer or further to test range.
4. **Play** with the control bar (section 4).
5. **Match end:** ENEMY DEFEATED / YOU WERE DEFEATED, then a new match after 3 s. RESET restarts at
   any time. Weapons and placement are kept across restarts.
6. A way back to weapon selection (e.g. a CHANGE WEAPONS button) without leaving demo mode.

## 4. Controls

**One control bar plus a switch.** The switch says who the bar controls.

*Built:* the buttons stand in for both players' wearables, so like `MockDataInjector` they call the demo
engine directly rather than going through Outbound (the wearables are on the engine's side of the
boundary). The attack row sits exactly where the HUD's cooldown icons are and shows the same sweeps, so
those icons are hidden in demo mode. CHANGE WEAPONS also asks for the enemy to be placed again, since the
selection screen hides the arena.

```
 [ YOU | ENEMY ]   [Horizontal] [Vertical] [Diagonal] [Thrust] [Spin] [Uppercut]   [ GUARD (hold) ]
```

- **6 attack buttons,** using the same icons as the cooldown row. Each sends that attack for the chosen
  side. A button on cooldown shows the grey sweep. Pressing it during cooldown does nothing, as in the
  real game (the engine's ON_COOLDOWN).
- **GUARD is hold-to-guard:** up while held, down on release, like raising your arm. That also makes
  parry timing demonstrable (guard raised ≤ 150 ms before the hit).
- **Controlling YOU:**
  - your first-person attack animation plays;
  - the outcome lands on the enemy's health;
  - holding GUARD blocks the enemy's attacks if you're facing it.
- **Controlling ENEMY:**
  - the enemy avatar performs the attack (animation, trail);
  - the outcome lands on **your** health ("YOU WERE HIT" / "YOU BLOCKED");
  - holding GUARD shows its guard pose (shield, or the two-handed bar for spear and mace).
- **The enemy only acts through the buttons.** No automatic guarding or side-stepping, so each action
  can be shown cleanly.
- **Layout:** the bar runs along the bottom edge, inside the view area (UI/ViewArea), clear of the health
  bars (top) and the cooldown icons (bottom left). Demo mode is meant for holding the phone, not for the
  headset.

## 5. Rules: the engine's resolver, ported

The same decision order as `comms/src/cg_comms/engine/resolver.py`:

1. **Match running?** If not, the attack is ignored.
2. **Valid move for the weapon?** All three weapons have all six moves since 2026-11-06.
3. **On cooldown for that move?** Then ON_COOLDOWN: no outcome; the first-person view doesn't play.
4. **Range:** the horizontal distance between the two players is compared with the move's `range_m`;
   beyond it is a **MISS**.
   - **You** are the phone camera, in arena coordinates (`PositionReporter`).
   - **The enemy** is where it was placed (arena origin).
5. **Guard:** the defender blocks only if guarding **and** facing the attacker within `guard_arc_deg`
   (120°).
   - **Your** facing is the camera heading.
   - **The enemy** always faces you.
6. **Parry:** a guard raised ≤ `parry_window_ms` (150 ms) before the hit takes 0 damage, and the
   attacker's running cooldowns are extended by `stagger_ms` (500 ms).
7. **Block:** `ceil(damage × block_multiplier)` (25%).
8. **Hit:** full `damage`, then the move's `cooldown_ms` starts (per move: each move only blocks itself).

**Not ported**, since they have no meaning with buttons:
- the AI confidence threshold;
- position staleness;
- the reorder buffer.

## 6. Rules source

- **Copied from `shared/config/weapons.yaml` and `match.yaml` into the app at build time,** like the
  engine token (`EngineTokenBuildStep`), and read at start-up.
- So the demo always plays with the same damage, range and cooldowns as the real engine. When the team
  retunes `weapons.yaml`, the next build follows automatically.
- If the files are missing (e.g. a checkout without `shared/`), the demo falls back to the copies last
  committed in the app.

## 7. What changes

| Piece | Change |
|---|---|
| `Offline/LocalGameRules.cs` | Becomes the **DemoEngine**: the resolver rules above, per-weapon data from the YAML, two independently controlled players |
| `Offline/MockDataInjector.cs` | Its automatic enemy behaviour is off in demo mode; the editor keys stay for `SampleScene` |
| New: demo control bar (UI) | Switch, 6 attack buttons, hold-to-guard; sends Outbound messages |
| `UI/WeaponSelectScreen.cs` | A button under each hologram; two steps (yours, then the enemy's) in demo mode |
| `Net/OnlineEngineClient.cs` | The DEMO MODE button: switch the online client off and the DemoEngine on (your teammate's file) |
| New: build step | Copies the two YAML files into `Resources/` (like the engine token) |
| Tests | Resolver cases (hit / miss by range / block / parry / cooldown); demo flow (select, place, attack both ways) |

## 8. Out of scope for now

- Demo mode in the headset (the buttons need the touchscreen).
- Moving the DEMO MODE button out of the connection panel.
- Per-weapon cooldown icons (the icons are the same for every weapon today).
