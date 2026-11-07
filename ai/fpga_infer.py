"""Ultra96 ACTION FPGA inference using two low-DDR AXI DMA packets.

Input protocol: one uint32 model ID followed by WINDOW*N_CH float32 values.
The HLS cnn_top reads a fixed number of stream words and ignores input TLAST.
This workaround was demonstrated on Ultra96 Rev1 with 1024+777 words.

This script does NOT update weights embedded in cnn.bit. Use the matching
ACTION_FINAL CPU weights for a meaningful bit-exact comparison.
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', 'src'))
import config as C
from fixedpoint_ref import FxCfg, forward_fixed, forward_float, load_weights, pad_out

LOW_DDR_END = 0x40000000
ERROR_BITS = 0x70
MM2S_SR, S2MM_SR = 0x04, 0x34


def _check_buffer(buf, label):
    address = int(buf.physical_address)
    end = address + int(buf.nbytes)
    if end > LOW_DDR_END:
        raise RuntimeError('%s buffer outside verified low DDR: 0x%x..0x%x' %
                           (label, address, end - 1))
    if address % 4:
        raise RuntimeError('%s buffer is not 4-byte aligned: 0x%x' % (label, address))
    return address


class FpgaEngine:
    def __init__(self, bitfile, dma_name=None, timeout_s=5.0):
        from pynq import Overlay, allocate
        self.ol = Overlay(bitfile, download=True)
        candidates = [k for k, v in self.ol.ip_dict.items()
                      if 'axi_dma' in v['type'].lower()]
        if not candidates:
            raise RuntimeError('No AXI DMA found in overlay')
        self.dma_name = dma_name or candidates[0]
        if self.dma_name not in candidates:
            raise ValueError('DMA %s not found; available: %s' %
                             (self.dma_name, candidates))
        self.dma = getattr(self.ol, self.dma_name)
        self.timeout = float(timeout_s)
        self.n_in = 1 + C.WINDOW * C.N_CH
        if self.n_in != 1801:
            raise ValueError('This tested split is for 1801 words; config requests %d' % self.n_in)
        self.part1 = None
        self.part2 = None
        self.outbuf = None
        try:
            self.part1 = allocate(shape=(1024,), dtype=np.uint32)
            self.part2 = allocate(shape=(777,), dtype=np.uint32)
            self.outbuf = allocate(shape=(C.NOUT,), dtype=np.float32)
            if C.NOUT != 7:
                raise ValueError('Expected seven output logits; got NOUT=%d' % C.NOUT)
            for name, buf in [('input1', self.part1), ('input2', self.part2),
                              ('output', self.outbuf)]:
                address = _check_buffer(buf, name)
                print('%s: %d bytes at 0x%x' % (name, buf.nbytes, address))
        except Exception:
            self.close()
            raise

    def close(self):
        for name in ('part1', 'part2', 'outbuf'):
            buf = getattr(self, name, None)
            if buf is not None:
                buf.freebuffer()
                setattr(self, name, None)

    def _wait_idle(self, status_offset, label):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            status = self.dma.mmio.read(status_offset)
            if status & ERROR_BITS:
                raise RuntimeError('%s DMA error: status=0x%08x' % (label, status))
            if status & 0x2:
                return
            time.sleep(0.001)
        mm2s = self.dma.mmio.read(MM2S_SR)
        s2mm = self.dma.mmio.read(S2MM_SR)
        raise TimeoutError('%s timeout: MM2S=0x%08x S2MM=0x%08x' %
                           (label, mm2s, s2mm))

    def infer(self, model_id, x_norm):
        x = np.asarray(x_norm, dtype=np.float32)
        if x.shape != (C.WINDOW, C.N_CH):
            raise ValueError('Expected shape %s, got %s' %
                             ((C.WINDOW, C.N_CH), x.shape))
        if not np.all(np.isfinite(x)):
            raise ValueError('Input contains NaN or infinity')
        words = np.empty((self.n_in,), dtype=np.uint32)
        words[0] = np.uint32(model_id)
        words[1:] = np.ascontiguousarray(x).ravel().view(np.uint32)
        self.part1[:] = words[:1024]
        self.part2[:] = words[1024:]
        self.outbuf[:] = np.nan
        # PYNQ DMA transfer() flushes send buffers; receive buffer is
        # invalidated after successful transfer before reading its contents.
        self.dma.recvchannel.transfer(self.outbuf)
        self.dma.sendchannel.transfer(self.part1)
        self._wait_idle(MM2S_SR, 'first MM2S packet')
        self.dma.sendchannel.transfer(self.part2)
        self._wait_idle(MM2S_SR, 'second MM2S packet')
        self._wait_idle(S2MM_SR, 'S2MM response')
        self.outbuf.invalidate()
        result = np.array(self.outbuf, dtype=np.float32, copy=True)
        if not np.all(np.isfinite(result)):
            raise RuntimeError('Non-finite FPGA logits: %s' % result)
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bit', required=True)
    ap.add_argument('--dma', default=None)
    ap.add_argument('--timeout', type=float, default=5.0)
    ap.add_argument('--testset', default=os.path.join(C.ROOT, 'hls', 'board_testset.npz'))
    ap.add_argument('--models-dir', default=C.MODELS_DIR)
    ap.add_argument('--models', nargs='+', default=['ACTION_FINAL'])
    ap.add_argument('--fx', default='16,8,16,4,40,18,20,2')
    ap.add_argument('--limit', type=int, default=10,
                    help='Maximum test windows per model; 0 means all')
    args = ap.parse_args()
    eng = FpgaEngine(args.bit, args.dma, args.timeout)
    try:
        print('Overlay loaded; DMA =', eng.dma_name)
        with np.load(args.testset) as ts:
            Xall = np.array(ts['X'])
            yall = np.array(ts['y'])
            ids = np.array(ts['model_id'])
        cfg = FxCfg(*[int(v) for v in args.fx.split(',')])
        for mid, name in enumerate(args.models):
            indices = np.where(ids == mid)[0]
            if args.limit > 0:
                indices = indices[:args.limit]
            if not len(indices):
                print('%s: no matching test windows' % name)
                continue
            X, y = Xall[indices], yall[indices]
            weights = pad_out(load_weights(os.path.join(args.models_dir, name, 'weights.npz')), C.NOUT)
            hw = []
            for i, x in enumerate(X):
                result = eng.infer(mid, x)
                hw.append(result)
                print('%s window %d: FPGA logits=%s' %
                      (name, i, np.round(result, 4).tolist()))
            hw = np.stack(hw)
            fx = np.asarray(forward_fixed(X, weights, cfg))
            fl = np.asarray(forward_float(X, weights))
            nv = len(C.CLASSES[name])
            print('%s: HW acc=%.4f fixed acc=%.4f float acc=%.4f bit-exact=%d/%d' %
                  (name, float((hw[:, :nv].argmax(1) == y).mean()),
                   float((fx[:, :nv].argmax(1) == y).mean()),
                   float((fl[:, :nv].argmax(1) == y).mean()),
                   int(np.array_equal(hw, fx)) * len(y) if hw.shape == fx.shape and np.array_equal(hw, fx)
                   else int(np.all(hw == fx, axis=1).sum()) if hw.shape == fx.shape else 0,
                   len(y)))
    finally:
        eng.close()


if __name__ == '__main__':
    main()
