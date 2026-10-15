# Hardware and visualizer integration

The online visualizer connects to the authoritative Python engine through a new adapter.
Hardware algorithms, firmware sketches, generated art, and the supplied offline scene are preserved.
The [integration plan](SUBSYSTEM_INTEGRATION_PLAN.md) tracks the remaining gateway and AI work.

## Implemented boundaries

- Online engine config: `comms/configs/engine.integration.yaml`.
- Complete `STATE_SNAPSHOT` at 10 Hz: both players' HP/max HP, nullable weapon, guard, valid
  positions, relay presence, readiness, per-action cooldowns, and displayed cooldown action.
- Snapshots on connect and periodically restore state without replaying combat events.
- `VISUALIZER_READY` is accepted only from the matching `/visualizer/1` or `/visualizer/2` path.
- Online play waits for both phones ready, both relays connected, selected weapons, and fresh
  positions. Missing readiness/relay presence/stale positions pauses combat and freezes cooldowns.
  Resume extends cooldown deadlines and excludes pre-resume attacks. Relay presence does not prove
  an independently connected physical gateway is healthy; gateway health remains a contract item.
- Reset stays on authenticated `/control`. Online reset clears combat/HP/cooldowns while retaining
  physical weapon selections and geometry; readiness still gates play. Phone placement/reset UI
  requests a display refresh, never a global reset.
- Explicit Unity enum/coordinate conversion. `COMBAT_EVENT` alone animates resolved attacks;
  `ACTION_RECOGNIZED` cannot double-animate them. Pure-idle guard release arrives in snapshots.
- Existing `engine.yaml` keeps its original auto-start behavior. New behavior is opt-in.

## Run with simulated hardware and AI

From `comms/`, supply `CG_ENGINE_BEARER_TOKEN` using the existing `.env.engine` or environment.
The launcher reads the token but does not print it. It starts the engine, two binary device/relay
sessions, and a JSON/TCP classifier simulator, leaving both visualizer paths free for phones.

```powershell
.\.venv\Scripts\python.exe -m cg_comms.tools.integration_mock --plain-local
```

This editor-only mode listens at `ws://127.0.0.1:8443`. Use that URL in Unity on the same computer.
The devices select SWORD; the canned classifier periodically returns a horizontal swing.
Sensor values and classification are simulated, not gesture recognition or radio validation.

For phones, run without `--plain-local` using the TLS files in `engine.integration.yaml`:

```powershell
.\.venv\Scripts\python.exe -m cg_comms.tools.integration_mock
```

Use a reachable host/IP matching the TLS certificate; `localhost` on a phone means the phone itself.
The Unity client uses platform certificate validation, with no accept-all override. Trust the
development certificate appropriately or use a trusted endpoint. A phone needs an actual network
route to the host; this does not provision hotspot/VPN/SSH routes. Use `--engine-host` if the
launcher's local relay connection needs a different certificate hostname.

For the real hardware gateway, use `relay.hardware.p1.yaml` / `relay.hardware.p2.yaml` with the regular `cg-relay` per player and the engine
alone with `configs/engine.integration.yaml`; do not run the mock launcher alongside them.

## Prepare the imported Unity project

If Unity Hub reports a missing editor, install **6000.5.10f1** using **Install Editor version**
in the project warning, or open the [official release page](https://unity.com/releases/editor/whats-new/6000.5.10f1)
and choose **Install** to launch Hub. Include **Android Build Support**, **Android SDK & NDK Tools**
and **OpenJDK** for the existing Android/Termux phone setup. Keep the project on its recorded editor;
the isolated checks with 6000.6.2f1 do not establish that upgrading the actual project is safe.
Unity documents the [Hub installation flow](https://docs.unity.com/en-us/hub/add-editor) and
[Android dependencies](https://docs.unity3d.com/6000.0/Documentation/Manual/android-install-dependencies.html).

1. Open `visualizer` in its recorded Unity **6000.5.10f1** environment.
   The manifest adds Newtonsoft Json 3.2.1; allow package resolution to finish.
2. Open **`Assets/Scenes/OnlineScene.unity`**, created during stage 2. It contains the online
   client/calibration and disables OfflineEngine. `SampleScene.unity` remains the offline scene.
   **CG4002 > Integration > Create Online Scene** also opens the existing online scene; if that
   file is absent, the menu creates it from SampleScene without changing the original.
3. OnlineScene is now first in the global build scene list; SampleScene and Showcase are retained.
   If using a build profile with its own scene-list override, make OnlineScene first there too.
4. In Play mode/on the phone, enter endpoint and bearer token, select P1 or P2, and Connect.
   The token is runtime-only: not serialized into the scene or saved in PlayerPrefs.
   Player selection also configures the opponent avatar. Use distinct IDs on the two phones.
5. For editor testing without an AR session, disable `requireARTracking` on SharedArenaCalibration.
   Keep it enabled for phones. Tracking loss stops readiness and online position sends.

Existing-script changes are boundary hooks: monotonic sample timestamps, runtime player assignment,
rehydrating an avatar after placement, optional placement-input blocking for the network overlay,
nullable weapon presentation, and complete/paused cooldown presentation. Models, animations,
offline combat rules, sensor algorithms, and existing scenes are not regenerated.

## Deployment sequence with real hardware and simulated AI

Use this sequence for the existing two Android phones and SSH/Termux network route.
Each long-running command needs its own terminal. Keep the existing local credentials,
`.env.engine`, `.env.relay`, certificates and Python environments on their respective machines.
The old assessment flags and throughput commands have been removed.

### 1. Update each checkout

From the repository root on the development laptop, relay laptop and Ultra96:

```sh
git lfs install
git pull --ff-only origin main
git lfs pull
```

Then from `comms/`, update the installed Python package:

```powershell
# Windows
.\.venv\Scripts\python.exe -m pip install -e .
```

```sh
# Ultra96
.venv/bin/python -m pip install -e .
```

If this is a fresh checkout, create its virtual environment first (`python -m venv .venv`
on Windows or `python3 -m venv .venv` on Linux, using Python 3.10 or newer).
Preserve or configure the machine-local environment files and certificates before starting services.

### 2. Flash the actual hardware profiles

Run `pio-venv\Scripts\pio.exe device list` from the repository root. Identify the physical
board and port for each role; the old demo's COM7/COM8 mapping does not establish the six-board mapping.
Verify board and flash-size settings against [the gateway instructions](../hardware/gateway/README.md).
From `hardware/gateway/`, flash each connected P1 board:

```powershell
$env:CG_GATEWAY_PSK_HEX = Read-Host "Existing P1 relay PSK (32 hex digits)"
$hubPort = Read-Host "P1 right-wrist FireBeetle COM port"
& "..\pio-venv\Scripts\pio.exe" run -e p1_hub -t upload --upload-port $hubPort
$torsoPort = Read-Host "P1 torso COM port"
& "..\pio-venv\Scripts\pio.exe" run -e p1_torso -t upload --upload-port $torsoPort
$leftPort = Read-Host "P1 left-wrist COM port"
& "..\pio-venv\Scripts\pio.exe" run -e p1_left -t upload --upload-port $leftPort
```

Repeat with the P2 key and profiles `p2_hub`, `p2_torso`, `p2_left`, using the actual P2 ports.
All six boards need the gateway builds for the new sample-export path.
The right wrist is the BLE hub; left wrist and torso keep Wi-Fi/UDP. Hardware Wi-Fi credentials
remain in the sketches. Close serial monitors before uploading.

### 3. Start Ultra96 services

Connect using the existing SSH command, then run each service from `comms/` in its own terminal:

```sh
# Classifier: IDLE-only while setting up and calibrating phones
.venv/bin/cg-ultra96-sim --host 127.0.0.1 --port 8888
```

```sh
# Authoritative engine
.venv/bin/cg-engine --config configs/engine.integration.yaml
```

Once setup is verified, stop just the classifier and restart with the repeatable action cycle:

```sh
.venv/bin/cg-ultra96-sim --host 127.0.0.1 --port 8888 --scenario configs/scenarios/demo_scenario.yaml
```

This simulator returns canned actions; it does not recognize the physical movements. No AI model
is integrated yet. Do not use the old button trigger map with these continuous hardware windows.

### 4. Start the relay laptop tunnel and both relays

Keep this tunnel running in its own terminal:

```sh
ssh -J e1138443@stujump.comp.nus.edu.sg -L 127.0.0.1:8888:127.0.0.1:8888 -L 127.0.0.1:8443:127.0.0.1:8443 xilinx@makerslab-fpga-53.ddns.comp.nus.edu.sg -N
```

From `comms/`, scan after flashing:

```powershell
.\.venv\Scripts\python.exe scripts\ble_scan.py
```

Find `CG4002-Hardware-P1` and `CG4002-Hardware-P2`. In a separate terminal for each player,
set that player's address and matching PSK (process-local overrides allow two relays in one checkout).
Retain the working engine token and CA certificate settings from `.env.relay`.

```powershell
# P1 terminal, from comms/
$env:CG_RELAY_TRANSPORT__BLE_ADDRESS = Read-Host "CG4002-Hardware-P1 BLE address"
$env:CG_RELAY_PSK_HEX = Read-Host "Existing P1 PSK used to flash p1_hub"
$env:CG_RELAY_ULTRA96__HOST = "127.0.0.1"
$env:CG_RELAY_ULTRA96__PORT = "8888"
.\.venv\Scripts\python.exe -m cg_comms.relay.app --config configs\relay.hardware.p1.yaml --no-tunnel --console-level INFO
```

In the P2 terminal use its address/key and `configs\relay.hardware.p2.yaml` instead.
The engine host must resolve to the local tunnel and match the existing TLS certificate
(keep the already-working localhost/loopback hostname and CA configuration).

### 5. Build and connect the phones

Install the matching Unity editor as described above. Add `visualizer` in Hub and open
`Assets/Scenes/OnlineScene.unity`. For Android, select **File > Build Profiles > Android**,
switch to that platform, and confirm OnlineScene is first in the active scene list.
Under **Project Settings > XR Plug-in Management > Android**, verify ARCore is enabled and
resolve Android/XR validation errors. Under Android Player settings, set **Internet Access**
to **Require** for the WebSocket client. Keep AR tracking required. Enable USB debugging on each
ARCore-supported phone, connect it by USB, then **Build And Run** for each device.
Actual phone builds and AR tracking still need validation on the matching editor.

On each Android phone, keep the same two Termux commands running in separate sessions:

```sh
ssh -J e1138443@stujump.comp.nus.edu.sg -L 127.0.0.1:8443:127.0.0.1:8443 xilinx@makerslab-fpga-53.ddns.comp.nus.edu.sg -N
```

```sh
socat TCP-LISTEN:8080,reuseaddr,fork OPENSSL:127.0.0.1:8443,verify=0
```

This retains the previous localhost bridge, including its existing disabled upstream certificate
verification. In the Unity app enter `ws://127.0.0.1:8080`, the existing engine bearer token,
and P1/P2 respectively, then Connect. The app appends `/visualizer/1` or `/visualizer/2` itself.
For a direct trusted WSS route instead, use its reachable certificate-matching endpoint.
iPhones require the iOS/ARKit build through a Mac/Xcode and a suitable network route; the Termux
steps apply only to Android.

### 6. Calibrate and test

Complete the common-origin/+Z calibration below on both phones and perform each player's physical
weapon-selection procedure. Both relay connections, weapon selections, phone readiness and fresh
positions are needed before the engine enters active play. Use **Show setup readings** to diagnose
waiting/paused conditions. After switching to the canned action cycle, reset from Ultra96 `comms/`:

```sh
.venv/bin/python -m cg_comms.tools.reset_match --config configs/engine.integration.yaml --host localhost
```

Check live samples, stale-wrist handling, reconnects and physical vibration using the gateway
acceptance checklist. Compilation and simulated tests do not establish physical radio/AR behavior.

## Calibrate the common arena

Mark one floor origin and a straight **+Z forward line** from it. Each phone must tap that exact
origin in AR. Then stand on the marked +Z side, at least 0.3 m away, and press **Confirm shared +Z
direction**. Repeat for the other phone using the same line/origin; move to playing positions
after calibration. Independent phone-facing placement is insufficient.

The adapter maps arena x/z to engine x/y, heading to `(90 - arenaYaw) mod 360`. Incoming opponent
positions put avatar feet at local y=0, not phone height. Moving/replacing the arena invalidates
calibration; tracking loss and backgrounding also require confirmation again. Reconfirmation clears
queued positions from the old coordinate frame and requests fresh engine readiness. Manual calibration is a first
integration procedure, not automatic shared-anchor tracking.

Verify a measured separation and facing in both phones before judging range/parry behavior.
Existing arrival-biased clock anchoring remains approximate; no sub-50 ms physical timing claim is made.

## Stage 3: physical phone check

Software preparation is complete; actual deployment/AR/network acceptance is still pending.
Only Unity 6000.6.2f1 is installed here, so the imported 6000.5.10f1 project has not been upgraded
or built onto a phone. The phone platform choice and matching build environment must be established
before deployment. Keep `requireARTracking` enabled on both phones.

Start with mock hardware/AI that remain idle, so combat cannot end the match during calibration:

```powershell
# From comms/, using the existing token and TLS configuration:
.\.venv\Scripts\python.exe -m cg_comms.tools.integration_mock --calibration-only
```

Use the reachable, certificate-matching WSS endpoint described above, with distinct P1/P2 identities.
No certificate-trust bypass was added. After common-origin/+Z calibration, tap **Show setup readings**
on both phones. The panel fits the screen safe area and shows the latest engine data:

- P1/P2 relay connection, readiness, position and heading;
- distance between their horizontal positions;
- snapshot age, phase and pause reason. Stale/disconnected data cannot show a current separation.

Check both phones at marked floor positions. For example, place P1 at the common origin and P2
one metre along the marked +Z line: readings should approach (0, 0) and (0, 1), separation 1 m.
Facing +Z corresponds to engine heading 90 degrees; facing -Z corresponds to 270 degrees. Record
the actual distance/heading errors and agree the acceptable AR tolerance before accepting the setup.
If either phone reports incompatible coordinates, repeat origin/direction placement.

| Physical check | Expected result | Evidence |
|---|---|---|
| Launch both builds | Online scene, distinct IDs, fresh snapshots | Pending |
| Same marks and facing | Consistent positions, separation and headings | Pending |
| Lose AR tracking | PAUSED; tracking recovery requires direction confirmation | Pending |
| Move/re-place arena | PAUSED; no automatic reuse of old calibration | Pending |
| Background a phone | Peer pauses; return requires confirmation, HP retained | Pending |
| Disconnect/reconnect | Fresh snapshot restores HP, no replayed attacks | Pending |
| Sustained movement | Fresh snapshots and agreed coordinate accuracy | Pending |

After geometry checks, stop the calibration-only launcher and restart it without that flag for
mock combat. A launcher restart starts a new simulated match. Verify damage/cooldowns/recovery,
then record phone models, OS versions, editor/build version, endpoint, measured errors and results
in this section. Do not record the bearer token or private key.

## Reset and recover

From `comms/`, reset via the existing authenticated operator command:

```powershell
.\.venv\Scripts\python.exe -m cg_comms.tools.reset_match --config configs/engine.integration.yaml --host localhost
```

For a `--plain-local` mock run, the reset tool must also use a config with `use_tls: false` and
the same endpoint/token. Do not send a TLS client to a plaintext test listener.
The phone's original RESET button only refreshes presentation online; it cannot reset HP.

The client reconnects with bounded buffers, clears stale connection traffic, requires fresh
snapshots, and applies Unity messages on the main thread. A reconnected phone must send fresh
readiness and positions before play resumes. Old attack events are not replayed.

## Gateway handoff

Comms connects over **BLE to one final FireBeetle per player**. The supplied two-player
implementation's right-wrist hub is used as that final board; internal communications stay
Wi-Fi/UDP. This boundary was specified by the user and does not need another transport decision.

The implementation and build/relay/physical acceptance steps are in
[`hardware/gateway/README.md`](../hardware/gateway/README.md). It builds the actual imported
sketches with small opt-in hooks, exports real left/torso samples internally, and reuses BLE v1
for sensor frames, locked weapon selection and queued engine haptic feedback. Unity needs no
additional changes for this gateway. AI model integration and physical acceptance remain open.

### Stage 4: capture the existing hub boundary

Implemented `cg_comms.tools.gateway_probe` and `cg_comms.relay.hardware_probe`. This is a
receive-only diagnostic path for the supplied two-player right-wrist hub. It never contacts the engine/AI, never transmits UDP packets,
and does not replace the production BLE Session or declare a gateway healthy for gameplay.

If the hardware owner confirms the selected board runs the supplied right-wrist sketch, put the
laptop on its network and use the hub's own IPv4 address. Close any other listener using the
same ports. Run from `comms/`:

```powershell
$gatewayIp = Read-Host "IPv4 address of the selected right-wrist hub"
.\.venv\Scripts\python.exe -m cg_comms.tools.gateway_probe --player 1 --gateway-ip $gatewayIp --duration 60 --out ../output/p1-hub.jsonl
.\.venv\Scripts\python.exe -m cg_comms.tools.gateway_probe --replay ../output/p1-hub.jsonl
```

For P2 use `--player 2`, its own hub IP, and a different output file. Existing captures are never
overwritten. Duration is limited to 600 seconds and captures to 8 MiB. Capture uses only the
selected source IP on POSE/RAW ports (4212/4214 for P1, 4222/4224 for P2). This source filter is
not packet authentication. Other board IPs are ignored; no per-board aggregation is introduced.

Each record preserves received bytes, source, stream and arrival offset, plus decoded fields or
the rejection reason. Replay re-decodes the bytes. A zero-packet run does not verify any interface.
Raw streams require a RAW_STREAM-enabled build; current supplied source defaults it to 1,
although the hardware README says 0. The running board's build still needs confirmation.

The decoder preserves hub-local uint32 millisecond timestamps and uint16 sequence numbers without
guessing boot/session identity or clock synchronization. It converts mg to g and 0.1 dps to dps,
labels samples `right_wrist`, and only treats PHASE_LOCKED as an actual weapon selection. It does
not create offhand samples, infer gesture confidence, or turn debug/result strings into actions.

The diagnostic probe remains optional. Production ingress now uses the BLE boundary in
`hardware/gateway`, not these diagnostic UDP packets. Internal aggregation stays on the hub;
the laptop relay does not attach to each sensor board. Segment-to-Ultra96 logging stubs remain
unchanged; their template distances are not used as classifier confidence.

AI must confirm sensor roles/window shape. Torso is not offhand, missing channels cannot be
zero-filled without agreement, and template distance is not classifier confidence. `spin`/`uppercut`
need agreed vocabulary/rules. Keep reserved wire IDs 0x40–0x4F untouched; version breaking layouts.

The hardware analysis tools also need the original missing `figpipe/raw.py` and `figpipe/replay.py`.
The ignore rule excluding the source package is fixed; missing algorithms have not been invented.

## Verification

Stage 4 implementation, **14 October 2026**:

- Six actual-hardware integration profiles compile: both hubs, both torsos and both left wrists.
  The original `comms/firmware` FireBeetle build also compiles with its default behavior.
  ELF symbol checks confirm BLE initialization is linked only in the two hubs, not the four peers.
- **286 Python regression tests passed**. Both new hardware relay configurations were loaded
  with environment-supplied BLE addresses and tokens; continuous mode and no implicit mock AI
  were verified.
- Six native C++ tests were added for sample roles, missing channels, scaling/clipping,
  timestamp/sequence wrap, identity rejection, duplicate/reorder rejection and node restart.
  Their C++ syntax compiles with the ESP32 compiler. They could not run: this machine lacks host gcc/g++. The ESP32 compiler is installed and
  was used for the six firmware builds. Install/use a host compiler to run `pio test -e native_test`.
- Build checks used a dummy PSK, never a deployment key. Rebuild each hub with its relay key
  before upload. No boards were flashed; radio coexistence, physical feedback, sampling alignment
  and two-player throughput remain physical acceptance items.


Stage 4 local checks, **13 October 2026**:

- **14 gateway probe tests passed**: fixed wire fixtures, units/roles, counter wrap preservation,
  candidate/locked selection, malformed and mismatched packets, two players, real localhost UDP
  capture/replay, source filtering, no response packets, and prevention of capture overwrite.
- New Python files pass Ruff; diff checks pass. Firmware, BLE packet IDs and existing production
  relay behavior are unchanged. No real hardware capture or feedback exchange was performed.
- Reproduce from `comms/`: `python -m pytest -q tests/test_gateway_probe.py`.

Stage 3 local checks, **13 October 2026**:

- **24 isolated Unity PlayMode tests passed**, including five calibration/setup-reading tests and
  two-client network coverage extended to pause/resume while calibration changes on an open socket.
- Checked calibration across translated/rotated AR worlds, rejection near the origin, tracking
  loss/recovery, origin movement, explicit invalidation, and missing-position diagnostics.
- Results: `output/unity-integration-validation/online-results-5c613636.xml`.
- **12 Python online integration tests passed**, including calibration-only mode retaining full HP
  with both phones ready and positions streaming. Changed Python files pass Ruff; diff checks pass.
- This remains a Unity 6000.6.2f1 / AR Foundation 6.6.2 host check, not phone AR acceptance.

Stage 2 checkpoint, **13 October 2026**:

- Created `Assets/Scenes/OnlineScene.unity` inside the existing imported Unity project, with a
  unique scene GUID. Compared against SampleScene: only the offline engine activation, added
  OnlineEngine object/components, and scene-root entry differ. Original SampleScene SHA-256:
  `3267aaab09b9b0eaa24a4c76bdad5fe4b43351c52c44e4fc7638a77dc5b522e6`.
- Checked the new scene's component references, offline-engine exclusion, AR tracking requirement,
  and absence of a serialized token. Scene rendering in the matching editor remains unverified.
- **19 isolated Unity PlayMode tests passed**, including two actual `OnlineEngineClient` instances
  communicating with the Python mock stack over authenticated localhost WebSockets. Verified
  readiness, combat/HP, one action event per outcome, pause on disconnect, snapshot recovery,
  and resume. AR poses are simulated in this test; it does not validate phone tracking or radios.
- Results: `output/unity-integration-validation/online-results-4c666c52.xml`.
  Validation still uses Unity 6000.6.2f1 / AR Foundation 6.6.2, leaving the imported version intact.
- No production runtime or hardware code changed during this stage. Next: open the online scene
  in the matching editor, then test two physical phones and shared-arena calibration.

Reproduce the isolated network check from `comms/` (optional editor executable argument):

```powershell
.\.venv\Scripts\python.exe scripts/prepare_unity_validation.py
.\.venv\Scripts\python.exe scripts/check_unity_online.py
```

The runner uses a temporary token and free loopback port, stops its mock stack/editor afterward,
and creates a fresh results XML for each run. Without this runner's environment variables, the
network test is skipped in the normal Unity Test Runner; other tests still run.

Recorded on 12 October 2026:

- Full Python regression: **271 passed** (260 existing plus 11 online integration cases).
- After the final snapshot-validation adjustment: **21 focused online/cooldown tests passed**.
- Unity **6000.6.2f1** isolated source compilation succeeded; **18 PlayMode tests passed**
  (12 existing state-engine tests and 6 new wire adapter tests). Results are in
  `output/unity-integration-validation/results.xml`. This used AR Foundation **6.6.2**,
  not the imported project's 6.5.1; matching-editor, scene, and phone validation remain open.
- Standalone C# wire adapter runner: **6 passed**. These are the same 6 adapter cases above.
- `git diff --check` passed. New Python modules/scripts/tests and the modified cooldown/inbound
  boundaries pass Ruff. Whole-repository Ruff and mypy remain non-clean; this is not a repository
  lint/type-cleanup change.

From `comms/`: `python -m pytest -q -p no:cacheprovider`. Online tests exercise actual authenticated
WebSockets, both players, guard release, combat, pause/resume, reconnect, reset, and the mock
launcher's binary sessions plus TCP classifier. Presentation tests check cooldown freeze/staleness.

From repo root: `scripts/check_visualizer_boundary.ps1` compiles and executes wire-codec tests with
installed Unity managed types; it is not a phone test. Run the imported project's PlayMode suite
in its matching editor too. `comms/scripts/prepare_unity_validation.py` prepares an isolated
source-validation project under ignored output/ for the installed editor without upgrading the
imported project; its AR package version differs from the target project's version.

Physical acceptance remains: two calibrated phones; actual gateway packet/feedback exchange;
restart each component independently; no duplicated damage, stuck guard or replayed attack;
then sustained two-player operation. Firmware and iOS/Android deployment are separate checks.
