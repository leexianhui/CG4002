"""
Comms <-> AI boundary: the classifier service the relay talks to (TCP, one JSON object per line).

Written by Comms during integration (2026-10-15). It reuses the AI team's own code unchanged
(fixedpoint_ref.forward_float/forward_fixed, fpga_infer.FpgaEngine, config) and only adapts the
message shapes. classifier_server.py is the AI team's original and is left as supplied; it cannot
run against this model drop (it needs SPEAR/MACE/GUARD model folders and a different request shape).

Contract (shared/protocol/app_messages.md 3.1/3.2, shared/schemas/sensor_window|action_result):
  in : {"type":"SENSOR_WINDOW","version":1,"timestamp":T,"playerId":1,"requestId":42,"weapon":"SWORD",
        "windowStartTs":..,"windowEndTs":..,"sampleRateHz":50,"stalePct":0,
        "dominant":[[ax,ay,az,gx,gy,gz],...],"offhand":[[...],...]}      accel in g, gyro in deg/s
  out: {"type":"ACTION_RESULT","version":1,"timestamp":T,"playerId":1,"requestId":42,
        "action":"HORIZONTAL_SWING","actionConfidence":0.94,"guardState":"IDLE","guardConfidence":0.0}
  timestamp, playerId and requestId MUST be echoed unchanged: the relay matches replies by requestId
  and rejects a reply whose timestamp/playerId changed.

Backends (--backend):
  float   numpy float model on the ARM cores (start here)
  fixed   bit-accurate fixed-point numpy model (what the FPGA computes)
  fpga    CNN accelerator via PYNQ/DMA (needs --bit; usually run as root in the PYNQ environment)
  stub    random answers, no model files needed (plumbing tests only)

  python3 comms_server.py --backend float
  python3 comms_server.py --backend fpga --bit cnn.bit
"""
import argparse
import asyncio
import json
import os
import random
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
from data_utils_lite import fit_window, load_norm_json  # noqa: E402
from fixedpoint_ref import FxCfg, forward_fixed, forward_float, load_weights, pad_out, softmax  # noqa: E402

PROTOCOL_VERSION = 1
WEAPONS = ("SWORD", "SPEAR", "MACE")
# Every action name the comms engine understands (comms/src/cg_comms/messages/enums.py ActionClass).
COMMS_ACTIONS = {"IDLE", "HORIZONTAL_SWING", "VERTICAL_SWING", "DIAGONAL_SWING", "THRUST",
                 "CHARGED_ATTACK", "CHARGED_THRUST", "SWING"}
# AI class -> comms action, for AI classes the engine has no move for. Reported as IDLE (no attack)
# until the team agrees a game meaning; the original label is still sent as "aiAction" for logs.
# To give one a meaning, map it here AND add the move to shared/config/weapons.yaml.
ACTION_MAP = {"SPIN_ATTACK": "IDLE", "UPPERCUT": "IDLE"}


class Model(object):
    """One trained CNN: weights.npz + norm.json (+ classes.json) from one directory."""

    def __init__(self, directory, name):
        self.name, self.directory = name, directory
        self.weights = load_weights(os.path.join(directory, "weights.npz"))
        self.mean, self.std = load_norm_json(os.path.join(directory, "norm.json"))
        with open(os.path.join(directory, "norm.json")) as f:
            self.window = int(json.load(f).get("window", C.WINDOW))
        classes_path = os.path.join(directory, "classes.json")
        if os.path.exists(classes_path):
            with open(classes_path) as f:
                self.classes = list(json.load(f)["classes"])
        else:
            self.classes = list(C.GUARD_CLASSES if name == "GUARD" else C.MOVE_CLASSES)
        n_out = self.weights["out_w"].shape[1]
        if n_out != len(self.classes):
            raise ValueError("%s: model has %d outputs but %d class names" % (directory, n_out, len(self.classes)))

    def normalise(self, samples):
        x = fit_window(samples, self.window)
        return ((x - self.mean) / self.std).astype(np.float32)


def find_models(models_dir):
    """Per-weapon folders (models/SWORD/...) if present, else one shared action model.

    The supplied drop has a single weapon-independent action model directly in models/ (its
    classes.json lists it as SWORD). A GUARD model is optional: without one, guard is reported IDLE.
    """
    def has_model(d):
        return os.path.exists(os.path.join(d, "weights.npz")) and os.path.exists(os.path.join(d, "norm.json"))

    shared = None
    for candidate in (models_dir, os.path.join(models_dir, "SWORD")):
        if has_model(candidate):
            shared = Model(candidate, "ACTION")
            break
    actions = {}
    for weapon in WEAPONS:
        d = os.path.join(models_dir, weapon)
        actions[weapon] = Model(d, weapon) if has_model(d) else shared
    if any(m is None for m in actions.values()):
        raise FileNotFoundError("no action model (weights.npz + norm.json) under %s" % models_dir)
    guard_dir = os.path.join(models_dir, "GUARD")
    guard = Model(guard_dir, "GUARD") if has_model(guard_dir) else None
    return actions, guard


class Backend(object):
    def __init__(self, kind, models_dir, bit=None, fx=None):
        self.kind = kind
        if kind == "stub":
            self.actions, self.guard = None, None
            return
        self.actions, self.guard = find_models(models_dir)
        self.cfg = FxCfg(*fx) if fx else FxCfg()
        self.fpga = None
        if kind == "fpga":
            if not bit:
                raise SystemExit("--bit is required for the fpga backend")
            from fpga_infer import FpgaEngine
            self.fpga = FpgaEngine(bit)
            # The supplied bitstream holds one model (model_id 0). Per-weapon bitstreams would need
            # their model_id order from export_hls.py --models.
            self.model_ids = {id(m): 0 for m in set(self.actions.values())}

    def logits(self, model, x):
        n = len(model.classes)
        if self.kind == "fpga" and id(model) in self.model_ids:
            return np.asarray(self.fpga.infer(self.model_ids[id(model)], x))[:n]
        if self.kind in ("fixed", "fpga"):
            return np.asarray(forward_fixed(x, pad_out(model.weights, C.NOUT), self.cfg))[:n]
        return np.asarray(forward_float(x, model.weights))[:n]

    def classify(self, model, samples):
        p = softmax(np.asarray(self.logits(model, model.normalise(samples)), dtype=np.float64))
        k = int(p.argmax())
        return model.classes[k], float(p[k])


def handle_request(backend, req, debug=False):
    """One SENSOR_WINDOW in, one ACTION_RESULT out (or an ERROR the relay logs and drops)."""
    if req.get("type") != "SENSOR_WINDOW":
        return {"type": "ERROR", "error": "unknown type %r" % req.get("type")}
    for key in ("timestamp", "playerId", "requestId", "weapon", "dominant", "offhand"):
        if key not in req:
            return {"type": "ERROR", "error": "missing %s" % key, "requestId": req.get("requestId")}
    weapon = str(req["weapon"]).upper()
    if weapon not in WEAPONS:
        return {"type": "ERROR", "error": "unknown weapon %r" % weapon, "requestId": req["requestId"]}
    t0 = time.perf_counter()
    if backend.kind == "stub":
        ai_action, a_conf = random.choice(sorted(COMMS_ACTIONS)), 0.9
        guard, g_conf = "IDLE", 0.0
    else:
        ai_action, a_conf = backend.classify(backend.actions[weapon], req["dominant"])
        if backend.guard is not None:
            guard, g_conf = backend.classify(backend.guard, req["offhand"])
        else:
            guard, g_conf = "IDLE", 0.0          # no guard model supplied yet
    action = ACTION_MAP.get(ai_action, ai_action)
    if action not in COMMS_ACTIONS:
        action = "IDLE"
    if guard not in ("IDLE", "GUARDING"):
        guard = "IDLE"
    out = {"type": "ACTION_RESULT", "version": PROTOCOL_VERSION,
           "timestamp": req["timestamp"], "playerId": req["playerId"], "requestId": req["requestId"],
           "action": action, "actionConfidence": round(a_conf, 4),
           "guardState": guard, "guardConfidence": round(g_conf, 4)}
    if ai_action != action:
        out["aiAction"] = ai_action
    if debug:
        out["latencyMs"] = round((time.perf_counter() - t0) * 1000, 3)
    return out


def make_client_handler(backend, debug):
    async def client(reader, writer):
        peer = writer.get_extra_info("peername")
        print("relay connected:", peer, flush=True)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    resp = handle_request(backend, json.loads(line.decode()), debug)
                except Exception as e:  # noqa: BLE001  one bad window must not kill the service
                    resp = {"type": "ERROR", "error": "%s: %s" % (type(e).__name__, e)}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            print("relay disconnected:", peer, flush=True)
    return client


async def serve(host, port, backend, debug):
    server = await asyncio.start_server(make_client_handler(backend, debug), host, port)
    if backend.kind == "stub":
        desc = "stub (random answers)"
    else:
        names = sorted({m.directory for m in backend.actions.values()})
        desc = "%s, action model(s) %s, window %d, guard %s" % (
            backend.kind, names, next(iter(backend.actions.values())).window,
            backend.guard.directory if backend.guard else "none (always IDLE)")
    print("AI classifier listening on %s:%d  [%s]" % (host, port, desc), flush=True)
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["float", "fixed", "fpga", "stub"], default="float")
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1: reachable only via the SSH tunnel")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--bit", default=None, help="cnn.bit (cnn.hwh must sit next to it)")
    ap.add_argument("--models-dir", default=os.path.join(HERE, "models"))
    ap.add_argument("--fx", default=None, help="fixed-point precision dw,di,ww,wi,aw,ai,rw,ri")
    ap.add_argument("--debug", action="store_true", help="add latencyMs to replies")
    a = ap.parse_args()
    fx = [int(v) for v in a.fx.split(",")] if a.fx else None
    backend = Backend(a.backend, a.models_dir, a.bit, fx)
    try:
        asyncio.run(serve(a.host, a.port, backend, a.debug))
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
