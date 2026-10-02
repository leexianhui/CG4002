# CG4002 Comms demo visualizer

Minimal Unity scene standing in for the real AR visualizer (`docs/REPO_PLAN.md`'s `visualizer/`,
owned by other teammates) for the 3-week Communications-subsystem demo. Not ARFoundation/camera
passthrough — a simple 3D top-down HUD scene proving the same JSON messages
`comms/src/cg_comms/mock/visualizer_sim.py` already logs are flowing end-to-end from a real ESP32
through the relay and engine to a phone.

## What exists here (code) vs. what you build (scene)

Handwriting a `.unity` scene file by hand isn't reliable — Unity's YAML format has cross-references
(GUIDs, file IDs) that are easy to get subtly wrong and hard to diagnose without the editor. So this
directory ships the actual C# scripts, and scene assembly is a short recipe you do once inside the
Unity Editor (10-15 minutes).

All under `CG4002VisualizerDemo_UnityTemp/My project/`:

- `Assets/Scripts/Net/AppMessages.cs` — C# models mirroring `shared/protocol/app_messages.md`
  (`COMBAT_EVENT`, `MATCH_STATE`, `COOLDOWN_STATE`, `WEAPON_CHANGED`, `POSITION`).
- `Assets/Scripts/Net/EngineClient.cs` — WebSocket client to `<ws|wss>://<host>:<port>/visualizer/<playerId>`
  (`UseTls` picks the scheme), matching `comms/src/cg_comms/engine/ws_server.py`'s auth/path
  contract and `visualizer_sim.py`'s connection behavior (bearer token header, periodic `POSITION`
  send, bounded reconnect loop).
- `Assets/Scripts/Controllers/DemoHudController.cs` — wires `EngineClient`'s events to HP bars,
  weapon labels, and a combat-event text feed.
- `Packages/manifest.json` — Unity Hub's default URP-template dependencies plus
  `com.unity.nuget.newtonsoft-json`, added for `EngineClient`/`AppMessages`.

This is the only Unity project in `visualizer_demo/`; the earlier hand-assembled scaffold was
removed once this one was verified on the phones.

## One-time setup

1. Install Unity 6 LTS (Unity Hub → matches `docs/REPO_PLAN.md`'s visualizer target; this project
   was created with 6000.6.2f1).
2. Open Unity Hub → **Add** → select `visualizer_demo/CG4002VisualizerDemo_UnityTemp/My project/`
   as an existing project (the actual Unity project root is the `My project` subfolder). This is a
   normal Unity Hub-generated project (URP template) with the demo's C# scripts copied into
   `Assets/Scripts/`.
   Unity will resolve `Packages/manifest.json` (pulls in Newtonsoft Json) on first open.
3. Get the engine's bearer token from whoever is running `cg-engine` (`CG_ENGINE_BEARER_TOKEN` in
   the engine host's `comms/.env.engine`). It is serialized into `Demo.unity` when you paste it
   into the Inspector. That is acceptable for this demo-only project (the token only gates the demo
   engine), but the real `visualizer/` must load it at runtime instead of baking it into a scene, and
   the token must be rotated in `.env.engine` and re-pasted if this repo is ever shared publicly.

## Building the scene (once)

1. **File → New Scene** → save as `Assets/Scenes/Demo.unity` (the project ships with Unity Hub's
   default `SampleScene.unity` — leave it alone rather than repurposing it, so a broken edit here
   never leaves you without a known-good scene to fall back to).
2. Create two avatars: **GameObject → 3D Object → Capsule**, name them `Player1Avatar` and
   `Player2Avatar`, position them a few metres apart (e.g. `(-2, 0.5, 0)` and `(2, 0.5, 0)`).
3. **GameObject → UI → Canvas** (Unity adds an EventSystem automatically). Under it add:
   - Two **Slider**s (`Player1HpBar`, `Player2HpBar`), range 0-100 (matches `starting_hp: 100` in
     the engine config), non-interactable.
   - Two **Text** (or TextMeshPro) labels (`Player1WeaponLabel`, `Player2WeaponLabel`) near each bar.
   - One **Text** box (`CombatFeedText`) large enough for ~8 lines, for the scrolling combat feed.
   - One **Text** label (`ConnectionStatusText`) showing connection state.
4. **GameObject → Create Empty**, name it `EngineClient`. Add Component → `EngineClient` (script).
   Set `Host` to `127.0.0.1`, `Port` `8080`, `UseTls` off, `PlayerId` to `1` or `2` depending on
   which phone this build is for, and `BearerToken` from the setup step above. The phone reaches the
   engine through two Termux sessions: an SSH `-J` forward of the engine's 8443, and
   `socat TCP-LISTEN:8080,reuseaddr,fork OPENSSL:127.0.0.1:8443,verify=0`, so Unity speaks
   plaintext to local socat and socat carries TLS through SSH (NOT the Ultra96's raw IP, which times
   out from a phone even on the NUS VPN). See `comms/DEMO_RUNBOOK.md` §8 for both commands.
5. **GameObject → Create Empty**, name it `HudController`. Add Component → `DemoHudController`.
   Drag the `EngineClient` object into its `Client` field, and drag each avatar/slider/label/text
   object created above into the matching field in the Inspector.
6. Save the scene, add it to **File → Build Settings → Scenes In Build**.

## Build & run

Build for whichever platform the phone actually is (Android/iOS) via **File → Build Settings**.
No AR packages, no camera permission needed — this is a plain 3D + UI scene.

## Known snag: self-signed TLS cert

`cg-engine`'s cert is self-signed (`comms/scripts/gen_certs.sh`). In the deployed socat route,
`verify=0` means socat does not verify it — say so accurately in the security walkthrough. The
`UseTls` + `PinnedCertSha256` direct-WSS mode still exists in `EngineClient` for a phone that can
reach 8443 itself; under IL2CPP the pinning callback may not be honored.

## Verification

Run `comms`'s mock rig first (`comms/README.md`) with `cg-visualizer-sim` to confirm the engine
itself is behaving; then point this Unity build at the same engine and confirm the HUD reflects the
same `COMBAT_EVENT`/`MATCH_STATE` traffic `cg-visualizer-sim`'s logs show.

## Preserve the working phone project

The active project is `CG4002VisualizerDemo_UnityTemp/My project`; its existing Demo.unity and
serialized phone SSH/socat/network values are authoritative. The generator now refuses to overwrite
an existing scene.

Rebuild the active APK for the bounded reconnect loop and Unity-yaw-to-canonical-heading adapter.
No scene regeneration is needed. Reset is an authenticated operator command on the engine host;
both visualizers receive the reset snapshot. See [runtime changes and verification](../docs/PRE_DEMO_ROBUSTNESS.md).
