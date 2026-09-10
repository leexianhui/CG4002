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

- `Assets/Scripts/Net/AppMessages.cs` — C# models mirroring `shared/protocol/app_messages.md`
  (`COMBAT_EVENT`, `MATCH_STATE`, `COOLDOWN_STATE`, `WEAPON_CHANGED`, `POSITION`).
- `Assets/Scripts/Net/EngineClient.cs` — WSS client to `wss://<host>:8443/visualizer/<playerId>`,
  matching `comms/src/cg_comms/engine/ws_server.py`'s auth/path contract and
  `visualizer_sim.py`'s connection behavior (bearer token header, periodic `POSITION` send).
- `Assets/Scripts/Controllers/DemoHudController.cs` — wires `EngineClient`'s events to HP bars,
  weapon labels, and a combat-event text feed.
- `Packages/manifest.json` — declares the `com.unity.nuget.newtonsoft-json` dependency.

## One-time setup

1. Install Unity 6 LTS (Unity Hub → matches `docs/REPO_PLAN.md`'s visualizer target).
2. Open Unity Hub → **Add** → select `visualizer_demo/CG4002VisualizerDemo/` as an existing
   project. Unity will resolve `Packages/manifest.json` (pulls in Newtonsoft Json) on first open.
3. Get the engine's bearer token and TLS cert fingerprint from whoever is running `cg-engine`:
   - Bearer token: `configs/engine.yaml`'s `bearer_token` (or `CG_ENGINE_BEARER_TOKEN` env var).
   - Cert fingerprint: `openssl x509 -noout -fingerprint -sha256 -in comms/certs/engine.crt`
     on the machine running the engine.

## Building the scene (once)

1. **File → New Scene** → save as `Assets/Scenes/Demo.unity`.
2. Create two avatars: **GameObject → 3D Object → Capsule**, name them `Player1Avatar` and
   `Player2Avatar`, position them a few metres apart (e.g. `(-2, 0.5, 0)` and `(2, 0.5, 0)`).
3. **GameObject → UI → Canvas** (Unity adds an EventSystem automatically). Under it add:
   - Two **Slider**s (`Player1HpBar`, `Player2HpBar`), range 0-50 (matches typical starting HP),
     non-interactable.
   - Two **Text** (or TextMeshPro) labels (`Player1WeaponLabel`, `Player2WeaponLabel`) near each bar.
   - One **Text** box (`CombatFeedText`) large enough for ~8 lines, for the scrolling combat feed.
   - One **Text** label (`ConnectionStatusText`) showing connection state.
4. **GameObject → Create Empty**, name it `EngineClient`. Add Component → `EngineClient` (script).
   Set `Host` to the Ultra96's demo-network IP, `Port` 8443, `PlayerId` to `1` or `2` depending on
   which phone this build is for, `BearerToken` and `PinnedCertSha256` from the setup step above.
5. **GameObject → Create Empty**, name it `HudController`. Add Component → `DemoHudController`.
   Drag the `EngineClient` object into its `Client` field, and drag each avatar/slider/label/text
   object created above into the matching field in the Inspector.
6. Save the scene, add it to **File → Build Settings → Scenes In Build**.

## Build & run

Build for whichever platform the phone actually is (Android/iOS) via **File → Build Settings**.
No AR packages, no camera permission needed — this is a plain 3D + UI scene.

## Known snag: self-signed TLS cert

`cg-engine`'s cert is self-signed (`comms/scripts/gen_certs.sh`). `EngineClient`'s
`PinnedCertSha256` field pins that exact cert via `ServicePointManager.ServerCertificateValidationCallback`,
which `System.Net.WebSockets.ClientWebSocket` honors under Mono. If a build targets IL2CPP and the
callback is not honored there, the fallback is to leave `PinnedCertSha256` blank (accepts any cert) —
acceptable for a closed demo network, but call this out explicitly if it comes up live; it is not
a fix, just a scoped-down risk for a one-off presentation.

## Verification

Run `comms`'s mock rig first (`comms/README.md`) with `cg-visualizer-sim` to confirm the engine
itself is behaving; then point this Unity build at the same engine and confirm the HUD reflects the
same `COMBAT_EVENT`/`MATCH_STATE` traffic `cg-visualizer-sim`'s logs show.
