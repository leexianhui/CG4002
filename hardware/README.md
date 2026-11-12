# Hardware workspace

Use this folder by purpose:

| Folder / files | Purpose |
|---|---|
| [`2 players/`](2%20players/) | Active six-board Arduino firmware and laptop listener |
| [`gateway/`](gateway/) | PlatformIO gateway integration, transport code, and native tests |
| [`figpipe/`](figpipe/) | Shared Python parsing, replay, fitting, and figure-generation library |
| [`tests/`](tests/) | Python tests and synthetic fixtures |
| [`diagnostics/`](diagnostics/) | Standalone one-board MPU, motor, and LED checks |
| Root `*.py` files | Analysis and recording tools; keep at the root because their documented commands and imports expect this layout |
| `requirements.txt` | Python dependencies for the hardware tools |
| `p2_attempts.csv` | Input data used by `lenient_p2.py` |
| `lenient_p1.py`, `p1_attempts.csv` | Builds the P1 fallback pattern bank and replays the hubs' accept decisions (commitment gate, guard suppression) on logged captures; the CSV lists the gated P1 forward strokes and whether each is in the bank |
| `guard_replay.py` | Replays and tunes the left-wrist guard detector on raw captures (`--sweep`) |

## Common entry points

Run Python commands from this `hardware` directory so the `figpipe` package resolves correctly.

```powershell
python listener.py --help
python "2 players/listener_2p.py" --help
python fit_strike_model.py --help
python make_figures.py --help
```

Open each board folder under `2 players/` in Arduino IDE, or build the gateway project from `gateway/` with PlatformIO.

The `diagnostics/` sketches are independent hardware checks and are not part of the six-board runtime firmware.
