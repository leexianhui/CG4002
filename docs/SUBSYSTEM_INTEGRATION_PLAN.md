# Subsystem integration plan

Integrate the imported hardware and visualizer at their boundaries, preserving their sensor,
actuator, rendering, and animation implementations. Comms owns the connection to one final
FireBeetle per player, not the internal links between that player's boards.

## Scope and decisions

- Keep both working comms demos. Imported subsystem roots are `hardware/` and
  `visualizer/`, with the redundant inner folders removed at the user's request.
- Add a Unity network adapter; retain MessageDispatcher, GameState, HUD, and avatar contracts.
- The Python engine remains authoritative for damage, range, guarding, and cooldowns.
- Online placement means readiness, never a global reset. Reset remains an operator command.
- Use explicit enum/coordinate conversions; never renumber Unity serialized enums.
- Keep BLE protocol v1 and reserved packet range 0x40–0x4F unchanged.
- The user confirmed BLE only at the final board-to-relay boundary; preserve internal Wi-Fi/UDP.
  Use the supplied right-wrist hub as that boundary. Real AI inputs still need the AI handoff.
  Do not manufacture offhand samples from torso data or treat a template distance as confidence.

## User-facing stages

| Stage | Scope | Current status |
|---|---|---|
| 1 | Comms/visualizer boundaries and mock launcher | Implemented and host-tested |
| 2 | Online Unity scene and real Unity clients against mocks | Implemented and host-tested |
| 3 | Phone deployment and shared-arena calibration | Software preparation implemented; physical acceptance pending |
| 4 | Final FireBeetle connection and feedback | Real sketch BLE/UDP boundary implemented; six profiles compile; physical acceptance pending |
| 5 | Real AI integration and final end-to-end acceptance | AI adapter implemented and host-tested (`ai/comms_server.py`); Ultra96/FPGA run and physical acceptance pending |

Three stages remain for complete physical integration, including stage 3. The detailed steps below
break down implementation work; they are not additional user-facing stages.

## Implementation steps

1. Preserve imports and baseline. Fix the ignored source-package rule. Record missing hardware
   analysis modules without recreating somebody else's algorithms.
2. Define gateway-to-relay contract: transport, player/session identity, sensor roles, units,
   clocks, continuous samples versus segments, weapon selection, and combat-feedback delivery.
   Obtain a real captured message. This stage gates physical hardware integration only.
3. Add Unity WebSocket transport, bounded queues, reconnects, explicit wire adapter, runtime
   connection settings, and a non-destructive online setup menu.
4. Add engine state snapshots and online readiness. Snapshots publish both players' weapons,
   HP/max HP, guard including release, positions, and per-action cooldowns. Reconnect must restore
   state without replaying attack events. Existing demo configuration retains automatic startup.
5. Add a manual shared-arena calibration boundary and configure both phone identities. Both phones
   must use the same physical floor origin and forward direction; independent tap placement is
   insufficient. Missing tracking/readiness pauses online combat.
6. Implement the agreed final gateway adapter and firmware send/feedback hooks after stage 2.
   Reuse Session and BLE v1 for the final hub; keep internal UDP on the boards.
   Preserve internal board topology and algorithms.
7. Validate the visualizer against the existing simulated classifier first. Replace the classifier
   only once its input shape and output vocabulary are agreed. Maintain request correlation.
8. Test two players, guard release, cooldowns, snapshot recovery, disconnects, reset, and duplicate
   prevention. Run Unity and physical board/phone checks separately from host tests.

## Completion record

AI integration and repo layout, 15 October 2026: the AI drop is `ai/`. Its own files are kept as
supplied; `ai/comms_server.py` adapts the relay's SENSOR_WINDOW/ACTION_RESULT contract to the AI
team's model code (float, bit-accurate fixed-point, or FPGA). `ai/data_utils_lite.py` is a
reconstruction of a module the drop imported but did not include. Hardware relay windows are now
100 samples / stride 10, the trained model's input length. AI-only classes (SPIN_ATTACK, UPPERCUT)
are reported as IDLE until given game rules. No GUARD model was supplied: guard is always IDLE.
The repo now has four subsystem roots (`ai/`, `comms/`, `hardware/`, `visualizer/`) plus `shared/`,
`docs/`, `scripts/`; `comms/firmware/` is the shared BLE stack and `hardware/gateway/` the six-board
build. The retired `visualizer_demo/` HUD was removed (history: 94108e4). 290 Python tests pass,
including 4 that run the relay's classifier client against the real model.

Deployment preparation, 14 October 2026: flattened both imported roots and updated firmware
includes, Unity validation tools and documentation. Hardware Wi-Fi credentials remain in the
teammate sketches as requested. Unity's Git LFS attribute macros are defined at the repository root so its assets can be published
and checked out correctly. Deployment machines need Git LFS plus their own secrets and certificates.

14 October 2026 correction: BLE at the final board was already the user's requirement. It is not
an unresolved transport choice. `hardware/gateway/` now builds the six actual two-player sketches
with opt-in boundary hooks. Right-wrist hubs alone use BLE; left/torso export real samples over
UDP. Locked posture selection becomes the existing weapon event; engine feedback drives hub
haptics. The left wrist keeps raw sampling after lock only in integration builds. See
[hardware build and acceptance instructions](../hardware/gateway/README.md). Physical validation
and the AI input contract remain open. Earlier interface-blocker entries below are superseded. All six hardware profiles and the
existing FireBeetle demo compile; 286 Python tests pass. Six new native C++ tests are pending
a host gcc/g++ toolchain (absent on this machine). No boards were flashed.

Stage 4 preparation, 13 October 2026: added a strict decoder and receive-only capture/replay tool
for the supplied right-wrist hub's existing UDP diagnostics. It filters one selected hub IP,
checks player/device identity, keeps device timestamps/roles, and distinguishes candidate versus
locked weapons. Fourteen decoder/localhost capture tests pass. This is not a gameplay ingress
adapter: no real gateway packets were available and the final FireBeetle interface remains
unidentified. Sensor forwarding and engine feedback stay pending. No hardware code was changed.

Stage 3 preparation, 13 October 2026: OnlineScene is first in the global build list; the online
panel displays calibration status, engine readiness/positions/headings, separation and snapshot
age. Tracking loss, moving the origin and backgrounding invalidate calibration. Reconfirmation
discards queued poses from the previous coordinate frame. A calibration-only mock mode keeps HP
unchanged during geometry checks. Physical phones and matching-editor deployment are still pending;
this stage is not marked physically complete. See the runbook for results and acceptance procedure.

User-facing staged checkpoint, 13 October 2026: **stage 2 (Unity scene and mock connection)**
is complete at the host-test level. The imported project now includes `Assets/Scenes/OnlineScene.unity`.
Two actual Unity clients passed combat, disconnect/pause, snapshot recovery and resume checks
against the Python mock stack (19 PlayMode tests total). The original scene and project version
are preserved. Next stage is matching-editor/phone validation and common-arena calibration.
These user-facing stages group the implementation steps above; gateway/AI handoffs remain open.

As of 12 October 2026: implementation steps 1, 3, 4, and 5 are implemented. Step 7 runs with a
simulated classifier. Step 8 passes host regression and isolated Unity source/PlayMode checks;
physical board/phone acceptance remains open. Steps 2 and 6 await the actual final gateway contract.
The real AI model remains a separate handoff. (Historical 12 October status; superseded above.)

Implementation and validation results are recorded in INTEGRATION_RUNBOOK.md. Steps 2 and 6 cannot
be certified from the supplied sketches: their segment-to-Ultra96 functions only log, and the
final FireBeetle's external interface has not been identified. The supplied raw stream covers
right wrist and torso at 100 Hz (mg and 0.1 degrees/second), unlike the current classifier input.
The hardware tools also reference absent figpipe/raw.py and figpipe/replay.py; obtain originals.

## Acceptance

- Existing comms tests continue to pass.
- Engine online tests cover real authenticated WebSocket connections and ordered gameplay.
- Unity adapter tests check mappings, coordinates, snapshots, and no duplicate animation.
- No sensor algorithms, generated art or existing scenes are rewritten. Board edits are limited
  to the necessary opt-in integration hooks, including continued left-wrist sample export.
- Physical completion requires two calibrated phones and the agreed gateway on actual hardware.
