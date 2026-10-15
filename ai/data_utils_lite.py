"""
STAND-IN written by Comms during integration (2026-10-15). The AI drop imports this module from
classifier_server.py, but the file itself was not included in AI_Integration_Codes. These two
functions are reconstructed from how classifier_server.py calls them; replace this file with the
AI team's original if theirs differs (especially fit_window's padding rule).

  load_norm_json(path) -> (mean, std)  per-channel arrays from models/<MODEL>/norm.json
  fit_window(samples, n) -> (n, 6) float32, last n samples, front-padded by repeating the first row
"""
import json

import numpy as np


def load_norm_json(path):
    with open(path) as f:
        d = json.load(f)
    mean = np.asarray(d["mean"], dtype=np.float32)
    std = np.asarray(d["std"], dtype=np.float32)
    std = np.where(std > 1e-6, std, 1.0).astype(np.float32)  # a dead channel must not divide by 0
    return mean, std


def fit_window(samples, n):
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim != 2 or x.shape[0] == 0:
        raise ValueError("expected a non-empty (T, channels) window, got shape %r" % (x.shape,))
    if x.shape[0] >= n:
        return x[-n:]
    pad = np.repeat(x[:1], n - x.shape[0], axis=0)
    return np.concatenate([pad, x], axis=0)
