"""
FPGA probe: what does the CNN core inside cnn.bit actually expect?

  sudo -E python3 fpga_probe.py            (from ai/, next to cnn.bit + cnn.hwh)

fpga_infer.py sends [model_id] + 100x18 float32 = 1801 words and waits for 7 float32 logits. If the
core was built for another input size, that DMA transfer hangs ("DMA timeout"). This finds the exact
number of input words the core consumes before it answers, by bisection on the board:

  input transfer done, reply not done  -> the core wants MORE words
  reply done, input transfer not done  -> the core wants FEWER words
  both done                            -> exact size

Every trial reprograms the bitstream, which resets the core and the DMA. Read-only otherwise.
If the size is 1801 it then checks the core against fixedpoint_ref on ACTION_FINAL's test windows.
"""
import argparse
import os
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MM2S_SR, S2MM_SR, S2MM_LENGTH = 0x04, 0x34, 0x58


def status(word):
    flags = []
    if word & 0x1: flags.append("halted")
    if word & 0x2: flags.append("idle")
    if word & 0x10: flags.append("INTERNAL_ERR")
    if word & 0x20: flags.append("SLAVE_ERR")
    if word & 0x40: flags.append("DECODE_ERR")
    return "%#06x(%s)" % (word, ",".join(flags) or "busy")


def trial(bit, words, timeout_s, fill=None):
    from pynq import Overlay, allocate
    ol = Overlay(bit, download=True)  # reprogram: a clean core and DMA every time
    dma = ol.axi_dma_0
    ib = allocate(shape=(words,), dtype=np.uint32)
    ob = allocate(shape=(64,), dtype=np.float32)
    ib[:] = 0 if fill is None else fill
    ob[:] = np.nan
    dma.recvchannel.transfer(ob)
    dma.sendchannel.transfer(ib)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if dma.read(MM2S_SR) & 0x2 and dma.read(S2MM_SR) & 0x2:
            break
    ms, ss = dma.read(MM2S_SR), dma.read(S2MM_SR)
    sent, replied = bool(ms & 0x2), bool(ss & 0x2)
    nbytes = dma.read(S2MM_LENGTH) if replied else 0
    out = np.array(ob[:nbytes // 4], dtype=np.float32)
    ib.freebuffer()
    ob.freebuffer()
    return sent, replied, ms, ss, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default=os.path.join(HERE, "cnn.bit"))
    ap.add_argument("--timeout", type=float, default=0.5)
    a = ap.parse_args()

    print("Bisecting the core's input size (each line reprograms the FPGA)...")
    lo, hi, found = 2, 4000, None
    while lo <= hi:
        n = (lo + hi) // 2
        sent, replied, ms, ss, out = trial(a.bit, n, a.timeout)
        print("  %5d words: input %-9s reply %-9s  MM2S=%s S2MM=%s reply_values=%d" % (
            n, "done" if sent else "STUCK", "done" if replied else "none", status(ms), status(ss), len(out)))
        if sent and replied:
            found = (n, out)
            break
        if sent:
            lo = n + 1
        elif replied:
            hi = n - 1
        else:
            print("  -> neither side finished at %d words: the core stopped reading without answering." % n)
            print("     Retrying the sizes it is supposed to take:")
            for m in (1801, 1800, 601, 600):
                s2, r2, ms2, ss2, out2 = trial(a.bit, m, a.timeout)
                print("     %5d words: input %-5s reply %-5s MM2S=%s S2MM=%s reply_values=%d" % (
                    m, "done" if s2 else "STUCK", "done" if r2 else "none", status(ms2), status(ss2), len(out2)))
            break

    if not found:
        print("\nRESULT: no input size completed cleanly. Send this whole output, plus the HLS source "
              "(the cnn_top function), to the AI team.")
        return
    n, out = found
    print("\nRESULT: the core takes exactly %d input words and returns %d values." % (n, len(out)))
    if n == 601:
        print("  = 1 model_id + 100 x 6: the OLD right-wrist-only CNN. The bitstream was rebuilt without the")
        print("    18-channel core: in Vivado, Report IP Status -> Upgrade cnn_top, then regenerate the bitstream.")
        return
    if n == 1800:
        print("  = 100 x 18 with no model_id word: 18 channels. fpga_infer.py must stop sending the model_id.")
        return
    if n != 1801:
        print("  = an unexpected layout (ACTION_FINAL needs 1 + 100 x 18 = 1801). Check the HLS top function.")
        return

    print("  = 1 model_id + 100 x 18: the ACTION_FINAL layout. Checking its answers against fixedpoint_ref...")
    import sys
    sys.path.insert(0, HERE)
    from fixedpoint_ref import FxCfg, forward_fixed, load_weights, pad_out
    W = pad_out(load_weights(os.path.join(HERE, "models", "ACTION_FINAL", "weights.npz")), 7)
    test = np.load(os.path.join(HERE, "models", "ACTION_FINAL", "test_data.npz"))
    exact = 0
    for i in range(10):
        x = test["X"][i].astype(np.float32)
        words = np.concatenate([[0], x.ravel().view(np.uint32)]).astype(np.uint32)
        sent, replied, ms, ss, hw = trial(a.bit, 1801, a.timeout, fill=words)
        ref = forward_fixed(x, W, FxCfg())
        same = replied and len(hw) == 7 and np.array_equal(hw, ref)
        exact += same
        print("  window %d: %s  hw=%s  ref=%s" % (i, "bit-exact" if same else "DIFFERENT",
                                                  np.round(hw, 3).tolist(), np.round(ref, 3).tolist()))
    print("\nRESULT: %d/10 windows bit-exact with ACTION_FINAL." % exact)


if __name__ == "__main__":
    main()
