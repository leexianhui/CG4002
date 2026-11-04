# ai/ — action classifier (runs on the Ultra96)

The AI team's final drop (2026-11-04): an 18-channel CNN on the FPGA. Guard is detected by the
hardware, so their GUARD_FINAL model was removed (the bitstream still has its model_id 1 slot,
unused). Their model code is unchanged. For integration, Comms changed only the message format in `classifier_server.py` and
`client_test.py`. See `AI_FILES_GUIDE.txt` for the AI team's own description.

| File | Purpose | Changed for integration? |
|---|---|---|
| `classifier_server.py` | **What you run.** TCP service on port 8888 for the relay | Boundary only: request/response fields, echoed IDs, comms action/guard names, missing sensors |
| `client_test.py` | Latency/sanity client, run through the SSH tunnel or on the board | Request format updated to the relay's; checks `requestId` comes back |
| `data_utils_lite.py` | `fit_window` / `load_norm_json` | **Added** by Comms (reconstruction) |
| `fixedpoint_ref.py`, `config.py` | CNN maths, channels / classes / model ids | no |
| `fpga_infer.py` | FPGA driver + self-test | Self-test defaults to ACTION_FINAL only |
| `cnn.bit`, `cnn.hwh` | FPGA bitstream (2026-11-04) + hardware description (Git LFS; keep them together) | no |
| `models/ACTION_FINAL/` | model_id 0: IDLE + 6 attacks, 94 % (64/68) on the AI team's test set | no |
| `hls/board_testset.npz` | ACTION_FINAL's held-out windows, the default input of `fpga_infer.py` | **Added** by Comms from `models/ACTION_FINAL/test_data.npz` |

## Run it (on the Ultra96, from `ai/`)

Needs Python 3 with `numpy` (already on PYNQ images). `git lfs pull` first, so `cnn.bit` is the real file.

```sh
python3 classifier_server.py --backend numpy           # float CNN on the ARM cores
python3 classifier_server.py --backend fixed           # bit-accurate copy of what the FPGA computes
sudo -E python3 fpga_infer.py --bit cnn.bit            # FPGA self-test: HW acc = fixed acc, bit-exact 68/68
sudo -E python3 classifier_server.py --backend fpga --bit cnn.bit   # serve from the FPGA
python3 classifier_server.py --backend stub            # random answers, no model: plumbing only
python3 client_test.py --n 50                          # in another terminal: checks replies + latency
```

Add `--debug` to the server to see `latencyMs` and `missingSensors` in each reply.
It listens on `127.0.0.1:8888`, the same port as `cg-ultra96-sim`. Run one or the other, never both.

## What the model takes, and where it comes from

One prediction per **(100, 18)** window: 100 consecutive rows at 50 Hz (2 s), oldest first, each row
right wrist `ax ay az gx gy gz`, left wrist the same, torso the same, in g and °/s. In the AI team's
training data the swing sits around the middle (rotation peaks at 0.8–1.2 s) with real motion on both
sides, and left wrist and torso carry real motion, so the model needs all three.

Since 2026-11-04 the hub builds exactly that for every action its segmenter accepts
(`hardware/gateway/README.md`, event mode): a 2 s window of all three boards with the stroke's midpoint
at 1.0 s. Before, only the right wrist reached this service, and the model got 39/68 instead of 64/68.

## The boundary with comms

The contract is `shared/protocol/app_messages.md` §3.1/§3.2.

- **In:** `dominant` (right wrist) → channels 0–5, `offhand` (left wrist) → 6–11, `torso` → 12–17,
  each `[ax, ay, az, gx, gy, gz]` in g and °/s. A sensor that is absent (`torso: null`), or a perfectly
  constant block (the relay's off-hand placeholder when the left wrist wasn't measured), is fed as the
  training mean (0 after normalisation). `samples` (100 × 18) is also accepted, for the AI team's tests.
- **Window:** 100 samples at 50 Hz (`config.WINDOW`); the hub sends exactly 100.
- **Out:** `requestId`, `playerId` and `timestamp` are echoed unchanged; the relay drops any reply
  where they differ.
- **Guard:** always `guardState: IDLE`, `guardConfidence: 0`. Guard comes from the hardware.
- **Action names:** an attack under 0.70 confidence becomes `IDLE`. Classes the game has no rules for
  are sent as `IDLE` via `COMMS_ACTION_MAP` (`SPIN_ATTACK`, `UPPERCUT`); the AI label stays in `rawAction`.

## Known gaps

1. **Hardware guard has no path to the game yet.** The AI no longer reports guard, and the hardware
   sketches don't detect it yet. When they do, it needs a way to the engine (e.g. a hub packet that
   the relay turns into an `ACTION` with `guardState: GUARDING`).
2. **Game meaning for `SPIN_ATTACK` and `UPPERCUT`.** Either map them in `COMMS_ACTION_MAP` or add
   the moves to `shared/config/weapons.yaml`.
3. **`cnn.hwh` is from the 2026-09-28 build.** Same `design_1`, part and DMA, so it should load the new
   bitstream, but ask the AI team for the matching `.hwh`. If `fpga_infer.py` isn't bit-exact, suspect this first.
4. **To confirm with the AI team:** how their 50 Hz training data was made from the boards' 100 Hz
   samples (the hub keeps the latest sample in each 20 ms step), and that it was recorded with the same
   board mounting and axes as the gateway builds.

Tested from `comms/` with `python -m pytest tests/test_ai_boundary.py`. It runs the relay's real
classifier client against `classifier_server.py`, and pushes the AI team's 68 test windows through the
whole relay path (int16 wire units, three-IMU chunks, reassembly, SENSOR_WINDOW JSON) in fixed point:
64/68, the same as the model itself.
