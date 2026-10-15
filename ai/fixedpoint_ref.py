"""
NumPy-only reference implementation of the network, in FLOAT and in
bit-accurate FIXED-POINT that mirrors hls/cnn.cpp exactly.

Why this file matters
  1. It IS the "mathematical basis of each layer" for the rubric (read forward_float()).
  2. It generates the expected outputs for the HLS testbench (C-sim must match it).
  3. It runs on the Ultra96 ARM cores as the software baseline (no TensorFlow needed).
  4. It lets you sweep precision (ap_fixed<W,I>) in seconds, before any synthesis.

Layer maths (T = window length, K = kernel size):
  Conv1D  : y[t,f] = b[f] + sum_{k<K} sum_{c<Cin} w[k,c,f] * x[t+k,c]      ('valid': t = 0..T-K)
  ReLU    : y = max(0, y)                       -> in hardware: a sign test + mux (no multiplier)
  GAP     : g[f] = (1/L) * sum_t y[t,f]         -> in hardware: adder chain * constant reciprocal
  Dense   : y[o] = b[o] + sum_i w[i,o] * x[i]
  Softmax : p[o] = exp(z[o]) / sum exp(z)       -> NOT on the FPGA: PL returns logits, PS does softmax
            (argmax(logits) == argmax(softmax), so the PL never needs exp/div)

Fixed-point semantics = Vitis ap_fixed<W,I,AP_RND,AP_SAT> for data/weights/recip and
ap_fixed<W,I> (AP_TRN, AP_WRAP) for the wide accumulator:
    value = round_half_up(x * 2^(W-I)) / 2^(W-I), saturated to [-2^(I-1), 2^(I-1) - 2^-(W-I)]
"""
import argparse
import csv
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import config as C  # noqa: E402


# ---------------------------------------------------------------- weights I/O
def load_weights(path):
    z = np.load(path)
    return {k: z[k].astype(np.float32) for k in z.files}


def softmax(z):
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


# ---------------------------------------------------------------- float model
def _conv1d(x, w, b):
    """x (N,T,Cin), w (K,Cin,Cout) -> (N,T-K+1,Cout)"""
    K = w.shape[0]
    lout = x.shape[1] - K + 1
    out = np.zeros((x.shape[0], lout, w.shape[2]), dtype=x.dtype)
    for k in range(K):                       # K small matmuls instead of an im2col copy
        out += x[:, k:k + lout, :] @ w[k]
    return out + b


def forward_float(X, W, return_all=False):
    """X (N,T,6) or (T,6). Returns logits (N,n_classes) [and dict of activations]."""
    single = X.ndim == 2
    x = X[None] if single else X
    a1 = np.maximum(_conv1d(x, W["conv1_w"], W["conv1_b"]), 0)
    a2 = np.maximum(_conv1d(a1, W["conv2_w"], W["conv2_b"]), 0)
    g = a2.mean(axis=1)
    h = np.maximum(g @ W["dense1_w"] + W["dense1_b"], 0)
    z = h @ W["out_w"] + W["out_b"]
    if single:
        z = z[0]
    if return_all:
        return z, dict(input=x, conv1=a1, conv2=a2, gap=g, dense1=h, logits=z)
    return z


# ---------------------------------------------------------------- fixed point
class FxCfg(object):
    """Precision configuration. d=data/activations, w=weights, a=accumulator, r=GAP reciprocal."""

    def __init__(self, dw=16, di=8, ww=16, wi=4, aw=40, ai=18, rw=20, ri=2):
        self.dw, self.di, self.ww, self.wi, self.aw, self.ai, self.rw, self.ri = dw, di, ww, wi, aw, ai, rw, ri

    def label(self):
        return "d<%d,%d> w<%d,%d>" % (self.dw, self.di, self.ww, self.wi)

    def acc_is_exact(self):
        return (self.aw - self.ai) >= (self.ww - self.wi) + (self.dw - self.di)


def quant(x, W, I, rnd=True, sat=True):
    """Emulate ap_fixed<W,I,AP_RND|AP_TRN,AP_SAT|AP_WRAP>."""
    s = 2.0 ** (W - I)
    q = np.floor(np.asarray(x, dtype=np.float64) * s + (0.5 if rnd else 0.0))
    lo, hi = -2.0 ** (W - 1), 2.0 ** (W - 1) - 1
    if sat:
        q = np.clip(q, lo, hi)
    else:
        q = ((q - lo) % (2.0 ** W)) + lo
    return q / s


def forward_fixed(X, W, cfg=None):
    """Bit-accurate emulation of hls/cnn.cpp.  Returns float32 logits (what the PL streams back)."""
    cfg = cfg or FxCfg()
    single = X.ndim == 2
    x = X[None] if single else X
    qd = lambda a: quant(a, cfg.dw, cfg.di, True, True)                 # noqa: E731
    qw = lambda a: quant(a, cfg.ww, cfg.wi, True, True)                 # noqa: E731
    qa = lambda a: quant(a, cfg.aw, cfg.ai, False, False)               # noqa: E731
    Wq = {k: qw(v) for k, v in W.items()}
    x = qd(x.astype(np.float32).astype(np.float64))                     # host sends float32
    a1 = qd(np.maximum(qa(_conv1d(x, Wq["conv1_w"], Wq["conv1_b"])), 0))
    a2 = qd(np.maximum(qa(_conv1d(a1, Wq["conv2_w"], Wq["conv2_b"])), 0))
    recip = quant(1.0 / a2.shape[1], cfg.rw, cfg.ri, True, True)
    g = qd(qa(a2.sum(axis=1)) * recip)
    h = qd(np.maximum(qa(g @ Wq["dense1_w"] + Wq["dense1_b"]), 0))
    z = qa(h @ Wq["out_w"] + Wq["out_b"]).astype(np.float32)
    return z[0] if single else z


def pad_out(W, n_out):
    """Pad the last layer with zero columns so every model has n_out logits (one IP for all models)."""
    W = dict(W)
    n = W["out_w"].shape[1]
    if n < n_out:
        W["out_w"] = np.pad(W["out_w"], ((0, 0), (0, n_out - n)))
        W["out_b"] = np.pad(W["out_b"], (0, n_out - n))
    return W


# ---------------------------------------------------------------- CLI helpers
def _load_test(model):
    d = os.path.join(C.MODELS_DIR, model)
    z = np.load(os.path.join(d, "test_data.npz"))
    return z["X"], z["y"], load_weights(os.path.join(d, "weights.npz"))


def cmd_profile(a):
    for model in a.models:
        X, y, W = _load_test(model)
        _, act = forward_float(X, W, return_all=True)
        print("\n== %s: value ranges on the test set (float) -> choose integer bits I >= ceil(log2(max))+1 ==" % model)
        for name, arr in act.items():
            m = float(np.abs(arr).max())
            print("  %-8s max|x| = %9.3f   -> I >= %d" % (name, m, int(np.ceil(np.log2(max(m, 1e-9)))) + 1))
        for k, v in W.items():
            if k.endswith("_w"):
                m = float(np.abs(v).max())
                print("  %-8s max|w| = %9.3f   -> I >= %d" % (k, m, int(np.ceil(np.log2(max(m, 1e-9)))) + 1))


def cmd_sweep(a):
    grid = [(16, 8, 16, 4), (16, 7, 16, 4), (16, 6, 16, 4), (16, 8, 12, 3), (14, 6, 14, 3), (12, 5, 12, 3), (12, 5, 10, 2),
            (10, 4, 10, 2), (8, 4, 8, 2), (8, 3, 8, 1)]
    os.makedirs(C.RESULTS_DIR, exist_ok=True)
    out = os.path.join(C.RESULTS_DIR, "precision_sweep.csv")
    rows = []
    for model in a.models:
        X, y, W = _load_test(model)
        zf = forward_float(X, W)
        acc_f = float((zf.argmax(1) == y).mean())
        print("\n%s  float accuracy = %.4f  (n=%d)" % (model, acc_f, len(y)))
        for (dw, di, ww, wi) in grid:
            cfg = FxCfg(dw, di, ww, wi)
            zq = forward_fixed(X, W, cfg)
            acc_q = float((zq.argmax(1) == y).mean())
            agree = float((zq.argmax(1) == zf.argmax(1)).mean())
            print("  %-20s acc=%.4f  agree-with-float=%.4f  acc_exact=%s" % (cfg.label(), acc_q, agree, cfg.acc_is_exact()))
            rows.append([model, cfg.label(), dw, di, ww, wi, len(y), acc_f, acc_q, agree])
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "precision", "dw", "di", "ww", "wi", "n_test", "acc_float", "acc_fixed", "agree_with_float"])
        w.writerows(rows)
    print("\nwritten", out)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.figure(figsize=(7, 4))
        for model in a.models:
            r = [x for x in rows if x[0] == model]
            plt.plot([x[1] for x in r], [x[8] for x in r], marker="o", label=model)
            plt.axhline(r[0][7], ls=":", lw=0.8, color="gray")
        plt.xticks(rotation=45, ha="right", fontsize=7)
        plt.ylabel("test accuracy")
        plt.title("Accuracy vs fixed-point precision (dotted = float)")
        plt.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(C.RESULTS_DIR, "precision_sweep.png"), dpi=150)
    except Exception as e:  # noqa: BLE001
        print("plot skipped:", e)


def cmd_check(a):
    cfg = FxCfg()
    for model in a.models:
        X, y, W = _load_test(model)
        zf, zq = forward_float(X, W), forward_fixed(X, W, cfg)
        print("%-6s float acc %.4f | fixed %s acc %.4f | max|logit diff| %.4f"
              % (model, (zf.argmax(1) == y).mean(), cfg.label(), (zq.argmax(1) == y).mean(), np.abs(zf - zq).max()))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("profile", cmd_profile), ("sweep", cmd_sweep), ("check", cmd_check)):
        p = sub.add_parser(name)
        p.add_argument("--models", nargs="+", default=C.MODEL_NAMES)
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)
