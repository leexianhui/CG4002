# ai/ — action + guard classifier (runs on the Ultra96)

The AI team's final drop (2026-11-04): two 18-channel CNNs on one FPGA IP. Their model code is
unchanged. For integration, Comms changed only the message format in `classifier_server.py` and
`client_test.py`. See `AI_FILES_GUIDE.txt` for the AI team's own description.

| File | Purpose | Changed for integration? |
|---|---|---|
| `classifier_server.py` | **What you run.** TCP service on port 8888 for the relay | Boundary only: request/response fields, echoed IDs, comms action/guard names, missing sensors |
| `client_test.py` | Latency/sanity client, run through the SSH tunnel or on the board | Request format updated to the relay's; checks `requestId` comes back |
| `data_utils_lite.py` | `fit_window` / `load_norm_json` | **Added** by Comms (reconstruction) |
| `fixedpoint_ref.py`, `fpga_infer.py`, `config.py` | CNN maths, FPGA driver + self-test, channels / classes / model ids | no |
| `cnn.bit`, `cnn.hwh` | FPGA bitstream (2026-11-04) + hardware description (Git LFS; keep them together) | no |
| `models/ACTION_FINAL/` | model_id 0: IDLE + 6 attacks, 94 % on the AI team's test set | no |
| `models/GUARD_FINAL/` | model_id 1: NOT_GUARDING / GUARDING, right + left wrist only | no |
| `hls/board_testset.npz` | Both models' held-out windows, the default input of `fpga_infer.py` | **Added** by Comms from `models/*/test_data.npz` |

## Run it (on the Ultra96, from `ai/`)

Needs Python 3 with `numpy` (already on PYNQ images). `git lfs pull` first, so `cnn.bit` is the real file.

```sh
python3 classifier_server.py --backend numpy           # float CNN on the ARM cores
python3 classifier_server.py --backend fixed           # bit-accurate copy of what the FPGA computes
sudo -E python3 fpga_infer.py --bit cnn.bit            # FPGA self-test: HW acc = fixed acc, bit-exact n/n
sudo -E python3 classifier_server.py --backend fpga --bit cnn.bit   # serve from the FPGA
python3 classifier_server.py --backend stub            # random answers, no model: plumbing only
python3 client_test.py --n 50                          # in another terminal: checks replies + latency
```

Add `--debug` to the server to see `latencyMs` and `missingSensors` in each reply.
It listens on `127.0.0.1:8888`, the same port as `cg-ultra96-sim`. Run one or the other, never both.

## The boundary with comms

The contract is `shared/protocol/app_messages.md` §3.1/§3.2.

- **In:** `dominant` (right wrist) → channels 0–5, `offhand` (left wrist) → 6–11, optional `torso` →
  12–17, each `[ax, ay, az, gx, gy, gz]` in g and °/s. A sensor that is absent, or a perfectly constant
  block (the relay's off-hand placeholder), is fed as the training mean (0 after normalisation).
  `samples` (100 × 18) is also accepted, for the AI team's own tests.
- **Window:** 100 samples at 50 Hz (`config.WINDOW`); `comms/configs/relay.hardware.p*.yaml` sends 100.
- **Out:** `requestId`, `playerId` and `timestamp` are echoed unchanged; the relay drops any reply
  where they differ.
- **Guard:** GUARD_FINAL with hysteresis (2 windows ≥ 0.55 to enter, 3 ≤ 0.35 to leave, per player).
  Guard has priority: while guarding the reply is `action: IDLE` + `guardState: GUARDING`.
  NOT_GUARDING is sent as `IDLE`, the comms name.
- **Action names:** an attack under 0.70 confidence becomes `IDLE`. Classes the game has no rules for
  are sent as `IDLE` via `COMMS_ACTION_MAP` (`SPIN_ATTACK`, `UPPERCUT`); the AI label stays in `rawAction`.

## Known gaps

1. **Segment mode sends only the right wrist.** Both models were trained on right + left + torso. On the
   AI team's test set, through this server in fixed point: 64/68 with all three IMUs, **39/68 with the
   right wrist only** (what the relay gets today). GUARD sees no left wrist at all, so it can't guard.
   Fix: the hub must put the left wrist and torso into its segments (`hardware/gateway`), and the relay
   must forward them as `offhand` / `torso`.
2. **Game meaning for `SPIN_ATTACK` and `UPPERCUT`.** Either map them in `COMMS_ACTION_MAP` or add
   the moves to `shared/config/weapons.yaml`.
3. **`cnn.hwh` is from the 2026-09-28 build.** Same `design_1`, part and DMA, so it should load the new
   bitstream, but ask the AI team for the matching `.hwh`. If `fpga_infer.py` isn't bit-exact, suspect this first.

Tested from `comms/` with `python -m pytest tests/test_ai_boundary.py`. It runs the relay's real
classifier client against `classifier_server.py` with the supplied models, in numpy and fixed point.
