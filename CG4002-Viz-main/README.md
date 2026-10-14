# CG4002 Visualizer

Unity 6.5 (6000.5.10f1) + AR Foundation 6.5 visualizer for the Group 23 AR duel, running on an
iPhone (ARKit). It draws a sword on a tracked cardboard prop and an opponent dummy placed on the
floor, plus a HUD. Every state change comes from Game Engine messages.

**Integrating Comms or the Game Engine? Read [Docs/StateEngine.md](Docs/StateEngine.md), sections 1, 2, 6 and 7.**

The integrated workspace uses `Assets/Scenes/OnlineScene.unity`; follow the
[integration runbook](../docs/INTEGRATION_RUNBOOK.md) for online deployment.
The imported archive lacked the contents of `Docs/PropMarkers.pdf` and four preview PNGs.
Their original paths, SHA-256 hashes and sizes are preserved in
[Docs/missing-assets.json](Docs/missing-assets.json) for recovery from the visualizer owner.
All runtime assets are present. The marker PDF must be recovered before using the printing
instruction below; the preview images are not required to build or run the project.

## How it connects

```
 visualizer ── dispatcher.Outbound ──▶  engine side  ── dispatcher.Inbound ──▶ visualizer
 (position, start-match requests)     (OfflineEngine now,     (health, guard, attacks,
                                        comms client later)     outcomes, cooldowns)
```

- The visualizer's whole boundary is these two thread-safe message queues on `MessageDispatcher`.
- **Offline mode** (the current default): the `OfflineEngine` GameObject holds `LocalGameRules`
  (stand-in engine), `MockDataInjector` (fake opponent and editor keys) and `SwordHitDetector`
  (stand-in for the IMU).
- **Online:** disable `OfflineEngine` and add a comms client that moves JSON between the
  network and the two queues. Nothing else changes.

## Running it

- **Editor:**
  1. Open `Assets/Scenes/SampleScene` and press Play (XR Simulation).
  2. Click a detected floor plane to place the dummy.
  3. Keys: **H** hit the dummy · **G** toggle its guard · **C** it hits you · **R** restart.
- **Phone:** File → Build Profiles → iOS → Build, then run from Xcode. Tap the floor to place
  the dummy and swing the prop into it. Print the marker from `Docs/PropMarkers.pdf` (9 cm).
- **Tests:** Window → General → Test Runner → PlayMode → Run All. Or headless:
  `Unity -batchmode -projectPath . -runTests -testPlatform PlayMode -testResults results.xml`

## Where things are

| Path | What |
|---|---|
| `Scripts/Runtime/State` | Messages, the two queues, `MessageDispatcher`, `GameState` |
| `Scripts/Runtime/Offline` | The stand-in engine side: disabled when online |
| `Scripts/Runtime/AR` | Prop tracking, tap-to-place, position reporting, sword trail, tracking overlay |
| `Scripts/Runtime/Controllers`, `UI` | Avatar and HUD: they only draw `GameState` |
| `Scripts/Runtime/Models`, `FX` | Avatar/weapon contracts, animation and effects |
| `Scripts/Runtime/Showcase` | Controls for the Showcase scene (avatar and weapon preview, no AR) |
| `Scripts/Editor` | Generators for all art, the scene setup, the contract validator |
| `Generated/` | Generator output. **Don't hand-edit**: it is overwritten |
| `Tests/PlayMode` | State engine, offline engine, hit detection and avatar tests |

All paths are under `Assets/CG4002/`.

## Docs

- [Docs/StateEngine.md](Docs/StateEngine.md): the state engine, the integration steps, the JSON wire format and the open team decisions.
- [Docs/PropTracking.md](Docs/PropTracking.md): world and image tracking, placing the dummy, reporting position, sword hit detection, limitations.

## Menu: CG4002

| Item | Does |
|---|---|
| **Generate Models** | Rebuilds everything in `Generated/` and the showcase scene |
| **Setup Image Tracking** | Builds the marker library and the AR tracking components |
| **Setup Game Scene** | Rebuilds `Game`, `OfflineEngine` and `HUD` in `SampleScene` and wires them |
| **Validate Model Contracts** | Checks anchors, clips and animation events after swapping art |
| **Render Preview Sheets** | PNGs of every key pose |

## Replacing the art

Controllers talk to models only through a fixed contract:
- **anchors:** `WeaponView.trailBase / trailTip / strikePoint`, `AvatarView` hand sockets;
- **commands:** `Equip`, `PlayAttack`, `SetGuard`;
- **events:** `StrikeOpened`, `StrikeClosed`, baked into the clips.

A new weapon mesh needs a `WeaponView` with the anchors placed. A new avatar needs an
`AvatarView` and clips carrying the same animation events. Neither needs a code change.
Afterwards, run **Validate Model Contracts**.
