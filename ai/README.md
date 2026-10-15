# ai/ — action classifier (runs on the Ultra96)

The AI team's drop (`AI_Integration_Codes`), kept as supplied, plus two files Comms added to
connect it to the relay. See `AI_FILES_GUIDE.txt` for the AI team's own description.

| File | Owner | Purpose |
|---|---|---|
| `comms_server.py` | Comms | **What you run.** TCP service on port 8888 that speaks the relay's `SENSOR_WINDOW` → `ACTION_RESULT` contract and calls the AI model code below |
| `data_utils_lite.py` | Comms (stand-in) | `fit_window` / `load_norm_json`, imported by the AI code but missing from the drop. Replace with the AI team's original if they send it |
| `classifier_server.py` | AI | The AI team's original server. Not used: it needs per-weapon + GUARD model folders and a different request shape |
| `fixedpoint_ref.py` | AI | CNN maths in float and bit-accurate fixed point |
| `fpga_infer.py` | AI | Runs the CNN on the FPGA via PYNQ + AXI DMA |
| `config.py` | AI | Class list, window size, model ids |
| `client_test.py` | AI | The AI team's own latency test client (uses their older request shape) |
| `cnn.bit`, `cnn.hwh` | AI | FPGA bitstream + hardware description (Git LFS; keep them together) |
| `models/` | AI | Trained weights, normalisation, class names |

## Run it (on the Ultra96, from `ai/`)

Needs Python 3 with `numpy` (already present on PYNQ images).

```sh
python3 comms_server.py --backend float            # CNN on the ARM cores: start here
python3 comms_server.py --backend fixed            # bit-accurate copy of what the FPGA computes
sudo -E python3 comms_server.py --backend fpga --bit cnn.bit   # real FPGA (PYNQ usually needs root)
python3 comms_server.py --backend stub             # random answers, no model: plumbing only
```

It listens on `127.0.0.1:8888`, the same port as `cg-ultra96-sim`. Run one or the other, never both.

## How it maps the contract

- **Input:** `dominant` (right wrist) goes to the action model. `offhand` (left wrist) goes to a GUARD
  model if `models/GUARD/` exists; otherwise guard is always `IDLE`. Accel is in g and gyro in °/s,
  the same units the model was trained on.
- **Window:** the model was trained on 100 samples (2 s at 50 Hz, `models/norm.json`).
  `comms/configs/relay.hardware.p*.yaml` sends exactly 100. A shorter window is front-padded and
  a longer one keeps the newest 100.
- **Output:** `requestId`, `timestamp` and `playerId` are echoed unchanged. The relay rejects the
  reply otherwise.
- **Vocabulary:** `SPIN_ATTACK` and `UPPERCUT` have no game rules in `shared/config/weapons.yaml`,
  so they are reported as `IDLE` (the original label is still sent as `aiAction` for logs). The
  engine also ignores moves a weapon doesn't have, such as `HORIZONTAL_SWING` with a MACE.
- **Weapons:** one weapon-independent action model serves SWORD, SPEAR and MACE. If per-weapon
  folders (`models/SPEAR/weights.npz` + `norm.json`) appear later, they are used automatically.

## Known gaps (AI team to supply)

1. No GUARD model, so the game never sees a guard from the AI.
2. The 18-channel (right + left + torso) model is not trained yet (see `AI_FILES_GUIDE.txt`). The
   hub already forwards the torso, but the relay sends only both wrists to the classifier.
3. Game rules are needed for `SPIN_ATTACK` and `UPPERCUT`. Add them to `ACTION_MAP` in
   `comms_server.py` and to `shared/config/weapons.yaml`.
4. `data_utils_lite.py` is a reconstruction (see above).

Tested from `comms/`: `python -m pytest tests/test_ai_boundary.py`. It runs the relay's real
classifier client against this server with the supplied model, in float and fixed-point.
