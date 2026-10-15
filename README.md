# CG4002 — AR Medieval Duel

Two players fight with IMU-instrumented weapons. Hardware senses the moves, the AI classifies them,
Comms carries everything and runs the authoritative game engine, and the AR visualizer shows the duel.

```text
 hardware/                comms/                          ai/                 visualizer/
 left wrist ─┐ Wi-Fi/UDP                                  (Ultra96)           (Android phones)
 torso ──────┼─────▶ right-wrist hub ══BLE══▶ relay ──TCP──▶ classifier
             │      (one per player)        (laptop)  ◀──── ACTION_RESULT
             │                                 │
             │                                 └──WSS──▶ game engine (Ultra96) ◀──WSS──▶ phone P1 / P2
             └◀── haptic feedback ◀════════════════════════ combat feedback
```

## Layout

| Folder | Subsystem | Start here |
|---|---|---|
| [`hardware/`](hardware/) | Sensor boards: weapon detection, IMU sampling, haptics | [`hardware/gateway/README.md`](hardware/gateway/README.md) builds and flashes all six boards |
| [`ai/`](ai/) | CNN action classifier, FPGA bitstream, trained model | [`ai/README.md`](ai/README.md) |
| [`comms/`](comms/) | Relay, game engine, mocks, the BLE firmware stack (`comms/firmware/`) | [`comms/README.md`](comms/README.md) |
| [`visualizer/`](visualizer/) | Unity 6000.5.10f1 AR client | [`visualizer/README.md`](visualizer/README.md) |
| [`shared/`](shared/) | The contract between all four: wire protocol, JSON schemas, game rules | [`shared/README.md`](shared/README.md) |
| [`docs/`](docs/) | Setup guide, runbooks, design report | **[`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md)** |
| [`scripts/`](scripts/) | Repo tools: pre-commit check, submission freeze | |

**To run the whole system, follow [`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md).**

## Ground rules

- Change `shared/` only with the other affected owners' agreement. Never renumber an enum or packet
  type, and packet types `0x40`–`0x4F` are reserved.
- Secrets never go in git: PSKs, bearer tokens, `.env.*` and `certs/` stay machine-local.
  Run `bash scripts/git_precommit_check.sh` before committing.
- Large binaries (`*.bit`, `*.hwh`, Unity models and textures) use Git LFS. Run `git lfs install` once per machine.
