"""
Classifier service for the Ultra96 -- the thing the Comms teammate's SSH tunnel (localhost:8888) talks to.
Line-delimited JSON, schema exactly as in report section 4.5:

  in : {"type":"SENSOR_WINDOW","playerId":1,"weapon":"SWORD","timestamp":12345678,
        "dominantWristSamples":[[ax,ay,az,gx,gy,gz],...],"offHandSamples":[[...],...]}
  out: {"type":"ACTION_RESULT","playerId":1,"weapon":"SWORD","action":"HORIZONTAL_SWING",
        "actionConfidence":0.94,"guardState":"IDLE","guardConfidence":0.88,"timestamp":12345680}

Backends (choose with --backend):
  stub    fixed/random answers, zero dependencies  -> give this to your teammates TODAY so they can integrate
  numpy   float model on the ARM cores (software baseline)
  fixed   bit-accurate fixed-point numpy model (what the FPGA computes)
  fpga    real hardware through PYNQ/DMA (needs --bit)

  python3 board/classifier_server.py --backend stub  --port 8888
  python3 board/classifier_server.py --backend numpy --port 8888
  python3 board/classifier_server.py --backend fpga  --bit ~/cnn/cnn.bit

Bind address is 127.0.0.1: the SSH tunnel (-L localhost:8888:localhost:8888) reaches it from inside the board,
nothing else on the network can.  Use --host 0.0.0.0 only for lab testing.
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
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import config as C  # noqa: E402
from data_utils_lite import fit_window, load_norm_json  # noqa: E402
from fixedpoint_ref import FxCfg, forward_fixed, forward_float, load_weights, pad_out, softmax  # noqa: E402


class StubBackend(object):
    def logits(self, model, x_raw):
        n = len(C.CLASSES[model])
        z = np.full(n, -2.0)
        z[random.randrange(n)] = 3.0
        return z


class NumpyBackend(object):
    def __init__(self, models_dir, fixed=False, fx=None):
        self.W = {n: load_weights(os.path.join(models_dir, n, "weights.npz")) for n in C.MODEL_NAMES}
        self.norm = {n: load_norm_json(os.path.join(models_dir, n, "norm.json")) for n in C.MODEL_NAMES}
        self.fixed, self.cfg = fixed, FxCfg(*fx) if fx else FxCfg()
        if fixed:
            self.W = {n: pad_out(w, C.NOUT) for n, w in self.W.items()}

    def logits(self, model, x_raw):
        m, s = self.norm[model]
        x = ((x_raw - m) / s).astype(np.float32)
        nv = len(C.CLASSES[model])
        z = forward_fixed(x, self.W[model], self.cfg) if self.fixed else forward_float(x, self.W[model])
        return np.asarray(z)[:nv]


class FpgaBackend(NumpyBackend):
    def __init__(self, models_dir, bit):
        NumpyBackend.__init__(self, models_dir)
        from fpga_infer import FpgaEngine
        self.eng = FpgaEngine(bit)

    def logits(self, model, x_raw):
        m, s = self.norm[model]
        x = ((x_raw - m) / s).astype(np.float32)
        return self.eng.infer(C.MODEL_IDS[model], x)[:len(C.CLASSES[model])]


def classify(backend, model, samples):
    x = fit_window(samples, C.WINDOW)
    p = softmax(np.asarray(backend.logits(model, x), dtype=np.float64))
    k = int(p.argmax())
    return C.CLASSES[model][k], float(p[k])


def handle_request(backend, req, debug=False):
    if req.get("type") == "PING":
        return {"type": "PONG", "timestamp": int(time.time() * 1000)}
    if req.get("type") != "SENSOR_WINDOW":
        return {"type": "ERROR", "error": "unknown type %r" % req.get("type")}
    weapon = str(req.get("weapon", "")).upper()
    if weapon not in C.WEAPONS:
        return {"type": "ERROR", "error": "unknown weapon %r" % weapon}
    t0 = time.perf_counter()
    action, a_conf = classify(backend, weapon, req["dominantWristSamples"])
    guard, g_conf = classify(backend, "GUARD", req["offHandSamples"])
    out = {"type": "ACTION_RESULT", "playerId": req.get("playerId"), "weapon": weapon, "action": action,
           "actionConfidence": round(a_conf, 4), "guardState": guard, "guardConfidence": round(g_conf, 4),
           "timestamp": int(time.time() * 1000)}
    if debug:
        out["latencyMs"] = round((time.perf_counter() - t0) * 1000, 3)
    return out


def make_server(backend, debug):
    async def client(reader, writer):
        peer = writer.get_extra_info("peername")
        print("client connected:", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    resp = handle_request(backend, json.loads(line.decode()), debug)
                except Exception as e:  # noqa: BLE001  (never let one bad packet kill the service)
                    resp = {"type": "ERROR", "error": "%s: %s" % (type(e).__name__, e)}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()
            print("client disconnected:", peer)
    return client


async def serve(host, port, backend, debug):
    await asyncio.start_server(make_server(backend, debug), host, port)
    print("classifier service listening on %s:%d  (backend=%s)" % (host, port, type(backend).__name__))
    await asyncio.Future()          # run until Ctrl-C  (works on Python 3.6 too, unlike `async with server`)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["stub", "numpy", "fixed", "fpga"], default="stub")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--bit", default=None)
    ap.add_argument("--models-dir", default=C.MODELS_DIR)
    ap.add_argument("--debug", action="store_true", help="add latencyMs to replies")
    a = ap.parse_args()
    if a.backend == "stub":
        be = StubBackend()
    elif a.backend in ("numpy", "fixed"):
        be = NumpyBackend(a.models_dir, fixed=(a.backend == "fixed"))
    else:
        if not a.bit:
            sys.exit("--bit required for the fpga backend")
        be = FpgaBackend(a.models_dir, a.bit)
    try:
        if hasattr(asyncio, "run"):
            asyncio.run(serve(a.host, a.port, be, a.debug))
        else:                                                   # Python 3.6 (older PYNQ images)
            asyncio.get_event_loop().run_until_complete(serve(a.host, a.port, be, a.debug))
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
