"""
FPGA inference on the Ultra96 through PYNQ + AXI DMA  (runs ON THE BOARD).

  python3 board/fpga_infer.py --bit ~/cnn/cnn.bit                     # verify + benchmark
  python3 board/fpga_infer.py --bit ~/cnn/cnn.bit --dma axi_dma_0 --n 5000 --rate 40

Needs cnn.bit AND cnn.hwh (same base name, same folder) from Vivado, plus
hls/board_testset.npz produced by src/export_hls.py.

What it measures (write these three numbers side by side in your report):
  * e2e latency  = fill input buffer + DMA in + PL compute + DMA out + read result   (what the game sees)
  * DMA+PL time  = the transfer/compute part only
  * pure IP latency = from the HLS/co-sim report (cycles * clock), NOT measured here

Protocol with the IP (see hls/cnn.h):  in = [model_id][WIN*NCH float32], out = NOUT float32 logits.

NOTE: written against the PYNQ v2.6/v3.0 API but not run on real hardware by the author of this template.
If a call fails, read the traceback and check the PYNQ docs for your image version.
"""
import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import config as C  # noqa: E402
from fixedpoint_ref import FxCfg, forward_fixed, forward_float, load_weights, pad_out  # noqa: E402


class FpgaEngine(object):
    def __init__(self, bitfile, dma_name=None, timeout_s=1.0):
        from pynq import Overlay, allocate          # imported here so the file can be syntax-checked off-board
        try:
            import pynq.lib.dma  # noqa: F401  (registers the AXI DMA driver so ol.axi_dma_0 has .sendchannel/.recvchannel)
        except Exception:  # noqa: BLE001
            pass
        self.ol = Overlay(bitfile)
        cands = [k for k, v in self.ol.ip_dict.items() if "axi_dma" in v["type"].lower()]
        if not cands:
            raise RuntimeError("no AXI DMA in overlay; ip_dict keys: %s" % list(self.ol.ip_dict))
        self.dma_name = dma_name or cands[0]
        self.dma = getattr(self.ol, self.dma_name)
        self.n_in = 1 + C.WINDOW * C.N_CH
        self.inbuf = allocate(shape=(self.n_in,), dtype=np.uint32)
        self.outbuf = allocate(shape=(C.NOUT,), dtype=np.float32)
        self.timeout = timeout_s

    def _wait(self, chan):
        t0 = time.perf_counter()
        while not chan.idle:
            if time.perf_counter() - t0 > self.timeout:
                raise RuntimeError("DMA timeout: IP produced no TLAST / wrong stream widths / IP not running")

    def infer(self, model_id, x_norm):
        """x_norm: (WINDOW,6) NORMALISED float32.  Returns NOUT float32 logits."""
        self.inbuf[0] = model_id
        self.inbuf[1:] = np.ascontiguousarray(x_norm, dtype=np.float32).ravel().view(np.uint32)
        self.dma.recvchannel.transfer(self.outbuf)     # arm the receive side first
        self.dma.sendchannel.transfer(self.inbuf)
        self._wait(self.dma.sendchannel)
        self._wait(self.dma.recvchannel)
        return np.array(self.outbuf, dtype=np.float32)

    def reset(self):
        try:
            self.dma.sendchannel.stop()
            self.dma.recvchannel.stop()
        except Exception:  # noqa: BLE001
            pass


def pct(a, p):
    return float(np.percentile(a, p))


def summarize(lat_s):
    a = np.asarray(lat_s) * 1e6
    return dict(n=len(a), mean_us=float(a.mean()), p50_us=pct(a, 50), p95_us=pct(a, 95), p99_us=pct(a, 99), max_us=float(a.max()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bit", required=True)
    ap.add_argument("--dma", default=None)
    ap.add_argument("--testset", default=os.path.join(C.ROOT, "hls", "board_testset.npz"))
    ap.add_argument("--models-dir", default=C.MODELS_DIR)
    ap.add_argument("--models", nargs="+", default=["SWORD"],
                    help="models baked into the bitstream, in export order (model_id 0..n-1). Must match the "
                         "--models used with src/export_hls.py (default: just SWORD).")
    ap.add_argument("--pl-mhz", type=float, default=None,
                    help="set the PL clock (MHz) after loading the bitstream, e.g. 100 = the frequency Vivado timed the design at")
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--rate", type=float, default=40.0)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--fx", default="16,8,16,4,40,18,20,2", help="precision the bitstream was built with: dw,di,ww,wi,aw,ai,rw,ri")
    ap.add_argument("--out", default=os.path.join(C.RESULTS_DIR, "bench_fpga.json"))
    a = ap.parse_args()

    eng = FpgaEngine(a.bit, a.dma)
    print("overlay loaded; DMA =", eng.dma_name)
    pl_actual = None
    try:
        from pynq import Clocks
        if a.pl_mhz:                      # loading an Overlay resets the PL clock, so set it AFTER loading
            Clocks.fclk0_mhz = a.pl_mhz
        pl_actual = float(Clocks.fclk0_mhz)
        print("PL clock fclk0 = %.1f MHz%s" % (pl_actual, " (set by --pl-mhz)" if a.pl_mhz else " (from the bitstream/hwh)"))
    except Exception as e:  # noqa: BLE001
        print("could not read/set PL clock:", e)
    cfg = FxCfg(*[int(v) for v in a.fx.split(",")])
    ts = np.load(a.testset)
    W = {n: pad_out(load_weights(os.path.join(a.models_dir, n, "weights.npz")), C.NOUT) for n in a.models}
    res = dict(bit=os.path.basename(a.bit), precision=cfg.label(), pl_mhz=pl_actual)

    # ---- 1. correctness: hardware vs bit-accurate numpy vs float ---------------------
    print("\n[1] correctness on the full test set")
    res["accuracy"] = {}
    tot_exact = tot = 0
    for mid, name in enumerate(a.models):
        sel = np.where(ts["model_id"] == mid)[0]
        nv = len(C.CLASSES[name])
        X, y = ts["X"][sel], ts["y"][sel]
        hw = np.stack([eng.infer(mid, x) for x in X])
        fx = forward_fixed(X, W[name], cfg)
        fl = forward_float(X, W[name])
        acc_hw = float((hw[:, :nv].argmax(1) == y).mean())
        acc_fx = float((fx[:, :nv].argmax(1) == y).mean())
        acc_fl = float((fl[:, :nv].argmax(1) == y).mean())
        exact = int((hw == fx).all(axis=1).sum())
        tot_exact += exact
        tot += len(y)
        res["accuracy"][name] = dict(n=int(len(y)), hw=acc_hw, numpy_fixed=acc_fx, float=acc_fl,
                                     bit_exact_vs_numpy=exact, max_abs_diff=float(np.abs(hw - fx).max()))
        print("  %-6s n=%4d | acc  HW %.4f  numpy-fixed %.4f  float %.4f | bit-exact %d/%d | max|diff| %.2g" %
              (name, len(y), acc_hw, acc_fx, acc_fl, exact, len(y), np.abs(hw - fx).max()))
    print("  overall bit-exact vectors: %d/%d" % (tot_exact, tot))

    # ---- 2. latency, back-to-back --------------------------------------------------------
    print("\n[2] latency (back-to-back), model SWORD")
    X0 = ts["X"][ts["model_id"] == 0]
    for i in range(200):
        eng.infer(0, X0[i % len(X0)])
    tot_l, fill_l, xfer_l = [], [], []
    for i in range(a.n):
        x = np.ascontiguousarray(X0[i % len(X0)], dtype=np.float32)
        t0 = time.perf_counter()
        eng.inbuf[0] = 0
        eng.inbuf[1:] = x.ravel().view(np.uint32)
        t1 = time.perf_counter()
        eng.dma.recvchannel.transfer(eng.outbuf)
        eng.dma.sendchannel.transfer(eng.inbuf)
        eng._wait(eng.dma.sendchannel)
        eng._wait(eng.dma.recvchannel)
        t2 = time.perf_counter()
        _ = np.array(eng.outbuf)
        t3 = time.perf_counter()
        fill_l.append(t1 - t0)
        xfer_l.append(t2 - t1)
        tot_l.append(t3 - t0)
    res["latency_b2b"] = dict(e2e=summarize(tot_l), fill=summarize(fill_l), dma_pl=summarize(xfer_l))
    for k, v in res["latency_b2b"].items():
        print("  %-7s mean %7.1f us  p50 %7.1f  p95 %7.1f  p99 %7.1f  max %7.1f" %
              (k, v["mean_us"], v["p50_us"], v["p95_us"], v["p99_us"], v["max_us"]))

    # ---- 3. sustained realistic load ---------------------------------------------------------
    print("\n[3] sustained %.0f inferences/s for %.0f s (cycling through %d model(s))" % (a.rate, a.seconds, len(a.models)))
    period, lat, miss, k = 1.0 / a.rate, [], 0, 0
    w0, c0 = time.time(), time.process_time()
    nxt = time.perf_counter()
    while time.time() - w0 < a.seconds:
        mid = k % len(a.models)
        Xm = ts["X"][ts["model_id"] == mid]
        t0 = time.perf_counter()
        eng.infer(mid, Xm[k % len(Xm)])
        dt = time.perf_counter() - t0
        lat.append(dt)
        miss += dt > period
        k += 1
        nxt += period
        sl = nxt - time.perf_counter()
        if sl > 0:
            time.sleep(sl)
    wall, cpu = time.time() - w0, time.process_time() - c0
    res["sustained"] = dict(summarize(lat), rate=a.rate, deadline_misses=int(miss), cpu_util_pct=100 * cpu / wall)
    s = res["sustained"]
    print("  mean %.1f us  p99 %.1f us  max %.1f us | deadline misses %d | CPU util %.2f%%" %
          (s["mean_us"], s["p99_us"], s["max_us"], miss, s["cpu_util_pct"]))

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(res, open(a.out, "w"), indent=2)
    print("\nwritten", a.out)


if __name__ == "__main__":
    main()
