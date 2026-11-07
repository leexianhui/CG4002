"""Diagnose AXI DMA transfers to the CG4002 CNN on Ultra96.

Run from ai/:
    sudo -E python3 fpga_probe.py
    sudo -E python3 fpga_probe.py --timeout 2
    sudo -E python3 fpga_probe.py --compare 10

This tool reloads the overlay for each trial. It does not change files or
boot configuration. Stop other applications using the FPGA before running.
"""
import argparse
import os
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MM2S_SR = 0x04
S2MM_SR = 0x34
S2MM_LENGTH = 0x58
ERROR_BITS = 0x70
REGISTERS = (
    ("MM2S_DMACR", 0x00), ("MM2S_DMASR", 0x04),
    ("MM2S_SA", 0x18), ("MM2S_SA_MSB", 0x1C),
    ("MM2S_LENGTH", 0x28), ("S2MM_DMACR", 0x30),
    ("S2MM_DMASR", 0x34), ("S2MM_DA", 0x48),
    ("S2MM_DA_MSB", 0x4C), ("S2MM_LENGTH", 0x58),
)


def status(word):
    flags = []
    for bit, label in ((0x1, "halted"), (0x2, "idle"),
                       (0x10, "INTERNAL_ERR"), (0x20, "SLAVE_ERR"),
                       (0x40, "DECODE_ERR"), (0x1000, "IOC_IRQ"),
                       (0x4000, "ERR_IRQ")):
        if word & bit:
            flags.append(label)
    return "0x%08x(%s)" % (word, ",".join(flags) or "busy")


def trial(bit, words, timeout_s, fill=None, verbose=True):
    from pynq import Overlay, allocate

    overlay = Overlay(bit, download=True)
    dma = overlay.axi_dma_0
    ib = allocate(shape=(words,), dtype=np.uint32)
    ob = allocate(shape=(64,), dtype=np.float32)
    try:
        if fill is None:
            ib.fill(0)
        else:
            payload = np.asarray(fill, dtype=np.uint32).reshape(-1)
            if payload.size != words:
                raise ValueError("Expected %d input words, got %d" % (words, payload.size))
            ib[:] = payload
        ob.fill(np.nan)
        ib.flush()
        ob.flush()

        input_address = int(ib.physical_address)
        output_address = int(ob.physical_address)
        if verbose:
            print("\n  INPUT  words=%d bytes=%d physical=%#x end=%#x" %
                  (words, ib.nbytes, input_address, input_address + ib.nbytes - 1))
            print("  OUTPUT capacity=%d bytes=%d physical=%#x end=%#x" %
                  (ob.size, ob.nbytes, output_address, output_address + ob.nbytes - 1))

        # Arm the receiver before sending input. Do not call .wait(): it can
        # block forever when the core or DMA is stuck.
        dma.recvchannel.transfer(ob)
        dma.sendchannel.transfer(ib)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            ms = dma.mmio.read(MM2S_SR)
            ss = dma.mmio.read(S2MM_SR)
            if (ms | ss) & ERROR_BITS:
                break
            if (ms & 0x2) and (ss & 0x2):
                break
            time.sleep(0.002)

        ms = dma.mmio.read(MM2S_SR)
        ss = dma.mmio.read(S2MM_SR)
        sent = bool(ms & 0x2) and not bool(ms & ERROR_BITS)
        replied = bool(ss & 0x2) and not bool(ss & ERROR_BITS)
        nbytes = int(dma.mmio.read(S2MM_LENGTH)) if replied else 0

        if verbose:
            print("  DMA registers immediately after transfer attempt:")
            for name, offset in REGISTERS:
                value = int(dma.mmio.read(offset))
                print("    %-17s 0x%08x%s" %
                      (name, value, " (%d bytes)" % value if name.endswith("LENGTH") else ""))
            print("  MM2S=%s S2MM=%s" % (status(ms), status(ss)))
            print("  Programmed source matches input:",
                  int(dma.mmio.read(0x18)) == (input_address & 0xFFFFFFFF))
            print("  Programmed destination matches output:",
                  int(dma.mmio.read(0x48)) == (output_address & 0xFFFFFFFF))

        if replied and 0 <= nbytes <= ob.nbytes and nbytes % 4 == 0:
            ob.invalidate()
            result = np.array(ob[:nbytes // 4], copy=True)
        else:
            if replied and verbose:
                print("  WARNING: invalid receive byte count:", nbytes)
            result = np.empty(0, dtype=np.float32)
        return sent, replied, ms, ss, result
    finally:
        ib.freebuffer()
        ob.freebuffer()


def compare_fixed(bit, timeout_s, count):
    from fixedpoint_ref import FxCfg, forward_fixed, load_weights, pad_out

    model_dir = os.path.join(HERE, "models", "ACTION_FINAL")
    weights_path = os.path.join(model_dir, "weights.npz")
    test_path = os.path.join(model_dir, "test_data.npz")
    if not (os.path.isfile(weights_path) and os.path.isfile(test_path)):
        print("Cannot compare: ACTION_FINAL weights.npz/test_data.npz missing")
        return
    weights = pad_out(load_weights(weights_path), 7)
    test = np.load(test_path)
    X = test["X"]
    exact = 0
    for i in range(min(count, len(X))):
        x = np.asarray(X[i], dtype=np.float32)
        if x.shape != (100, 18):
            print("Unexpected test shape:", x.shape)
            return
        payload = np.concatenate((np.array([0], dtype=np.uint32),
                                  x.reshape(-1).view(np.uint32)))
        sent, replied, ms, ss, hw = trial(bit, 1801, timeout_s, fill=payload,
                                          verbose=(i == 0))
        if not (sent and replied and len(hw) == 7):
            print("Window %d: DMA incomplete, stopping comparison" % i)
            break
        ref = np.asarray(forward_fixed(x, weights, FxCfg())).reshape(-1)
        same = ref.shape == hw.shape and np.array_equal(hw, ref)
        exact += int(same)
        print("Window %d: %s; FPGA=%s reference=%s" %
              (i, "bit-exact" if same else "DIFFERENT", hw.tolist(), ref.tolist()))
    print("Bit-exact windows:", exact)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bit", default=os.path.join(HERE, "cnn.bit"))
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--compare", type=int, default=0,
                        help="Compare N saved windows if 1801-word DMA succeeds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    print("Testing expected 18-channel protocol first: 1 + 100*18 = 1801 words")
    sent, replied, ms, ss, out = trial(args.bit, 1801, args.timeout)
    print("1801 words: input=%s reply=%s outputs=%d" %
          ("done" if sent else "STUCK", "done" if replied else "none", len(out)))
    if (ms | ss) & ERROR_BITS:
        print("\nDMA error detected. This is a memory/interconnect/transfer issue; "
              "input-size bisection would not be meaningful.")
        return
    if sent and replied:
        print("FPGA returned:", out.tolist())
        if len(out) != 7:
            print("WARNING: expected 7 logits, received", len(out))
        elif args.compare:
            compare_fixed(args.bit, args.timeout, args.compare)
        return

    print("\nNo DMA error, but transfer incomplete. Testing 601 words as a diagnostic.")
    sent, replied, ms, ss, out = trial(args.bit, 601, args.timeout)
    print("601 words: input=%s reply=%s outputs=%d" %
          ("done" if sent else "STUCK", "done" if replied else "none", len(out)))
    print("A completed MM2S transfer alone does not identify the CNN input size.")


if __name__ == "__main__":
    main()
