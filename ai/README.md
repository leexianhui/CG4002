# ai/ — action classifier (runs on the Ultra96)

The AI team's drop (`AI_Integration_Codes`). Their model code is unchanged. For integration, Comms
changed only the parts that touch other subsystems: the message format in `classifier_server.py`,
the request format in `client_test.py`, and the model folder layout. See `AI_FILES_GUIDE.txt` for
the AI team's own description.

| File | Purpose | Changed for integration? |
|---|---|---|
| `classifier_server.py` | **What you run.** TCP service on port 8888 for the relay | Boundary only: request/response fields, echoed IDs, action names, load only the models that exist |
| `client_test.py` | Latency/sanity client, run through the SSH tunnel or on the board | Request format updated to the relay's; checks `requestId` comes back |
| `data_utils_lite.py` | `fit_window` / `load_norm_json` | **Added**: imported by the server but missing from the drop. Replace with the AI team's original |
| `fixedpoint_ref.py`, `fpga_infer.py`, `config.py` | CNN maths, FPGA driver, class list / window size | no |
| `cnn.bit`, `cnn.hwh` | FPGA bitstream + hardware description (Git LFS; keep them together) | no |
| `models/SWORD/` | Trained weights, normalisation, class names | Moved into `models/<NAME>/`, the layout `classifier_server.py` and `fpga_infer.py` already expect |

## Run it (on the Ultra96, from `ai/`)

Needs Python 3 with `numpy` (already on PYNQ images).

```sh
python3 classifier_server.py --backend numpy           # float CNN on the ARM cores: start here
python3 classifier_server.py --backend fixed           # bit-accurate copy of what the FPGA computes
sudo -E python3 classifier_server.py --backend fpga --bit cnn.bit   # real FPGA (PYNQ usually needs root)
python3 classifier_server.py --backend stub            # random answers, no model: plumbing only
python3 client_test.py --n 50                          # in another terminal: checks replies + latency
```

It listens on `127.0.0.1:8888`, the same port as `cg-ultra96-sim`. Run one or the other, never both.

## The boundary with comms

The contract is `shared/protocol/app_messages.md` §3.1/§3.2 and `shared/schemas/sensor_window.schema.json` / `action_result.schema.json`.

- **In:** `dominant` (right wrist) goes to the action model and `offhand` (left wrist) to the GUARD
  model. Each is a list of `[ax, ay, az, gx, gy, gz]` in g and °/s, the units the model was trained on.
- **Window:** the model takes 100 samples (2 s at 50 Hz, `models/SWORD/norm.json`), and
  `comms/configs/relay.hardware.p*.yaml` sends exactly 100.
- **Out:** `requestId`, `playerId` and `timestamp` are echoed unchanged; the relay drops any reply
  where they differ.
- **Action names:** classes the game has no rules for are reported as `IDLE` via `COMMS_ACTION_MAP`
  (currently `SPIN_ATTACK` and `UPPERCUT`). The original label is still sent as `aiAction`, so it
  shows up in logs.
- **Models:** only folders that exist under `models/` are loaded. A weapon without its own folder
  uses `SWORD` (the current single action model). Without `models/GUARD/`, guard is `IDLE`. Dropping
  in `models/SPEAR/`, `models/MACE/` or `models/GUARD/` later needs no code change. The FPGA
  backend's `model_id` order (`config.MODEL_IDS`) must match how the bitstream was exported.

## Known gaps (AI team to supply)

1. **No GUARD model,** so the game never sees a guard from the AI.
2. **The 18-channel (right + left + torso) model is not trained yet.** The hub already forwards the
   torso, but the relay sends the classifier only the two wrists.
3. **Game meaning for `SPIN_ATTACK` and `UPPERCUT`.** Either map them in `COMMS_ACTION_MAP` or add
   the moves to `shared/config/weapons.yaml`.
4. **`data_utils_lite.py`** is a reconstruction, as noted above.

Tested from `comms/` with `python -m pytest tests/test_ai_boundary.py`. It runs the relay's real
classifier client against `classifier_server.py` with the supplied model, in numpy and fixed-point.
