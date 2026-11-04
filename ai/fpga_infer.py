"""
Final FPGA inference helper for ACTION_FINAL + GUARD_FINAL.

Protocol:
  input stream  = [model_id][100*18 normalized float32]
  output stream = 7 float32 logits

model_id 0 -> ACTION_FINAL
model_id 1 -> GUARD_FINAL
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
import config as C
from fixedpoint_ref import FxCfg, forward_fixed, forward_float, load_weights, pad_out

class FpgaEngine(object):
    def __init__(self, bitfile, dma_name=None, timeout_s=1.0):
        from pynq import Overlay, allocate
        try:
            import pynq.lib.dma
        except Exception:
            pass
        self.ol = Overlay(bitfile)
        cands = [k for k,v in self.ol.ip_dict.items() if "axi_dma" in v["type"].lower()]
        if not cands:
            raise RuntimeError("no AXI DMA in overlay")
        self.dma_name = dma_name or cands[0]
        self.dma = getattr(self.ol, self.dma_name)
        self.n_in = 1 + C.WINDOW * C.N_CH
        self.inbuf = allocate(shape=(self.n_in,), dtype=np.uint32)
        self.outbuf = allocate(shape=(C.NOUT,), dtype=np.float32)
        self.timeout = timeout_s

    def _wait(self, chan):
        t0=time.perf_counter()
        while not chan.idle:
            if time.perf_counter()-t0 > self.timeout:
                raise RuntimeError("DMA timeout")

    def infer(self, model_id, x_norm):
        x_norm=np.asarray(x_norm,np.float32)
        if x_norm.shape != (C.WINDOW,C.N_CH):
            raise ValueError("expected %s, got %s" % ((C.WINDOW,C.N_CH),x_norm.shape))
        self.inbuf[0] = model_id
        self.inbuf[1:] = np.ascontiguousarray(x_norm).ravel().view(np.uint32)
        self.dma.recvchannel.transfer(self.outbuf)
        self.dma.sendchannel.transfer(self.inbuf)
        self._wait(self.dma.sendchannel)
        self._wait(self.dma.recvchannel)
        return np.array(self.outbuf,dtype=np.float32)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--bit",required=True)
    ap.add_argument("--dma",default=None)
    ap.add_argument("--testset",default=os.path.join(C.ROOT,"hls","board_testset.npz"))
    ap.add_argument("--models-dir",default=C.MODELS_DIR)
    ap.add_argument("--models",nargs="+",default=["ACTION_FINAL","GUARD_FINAL"])
    ap.add_argument("--fx",default="16,8,16,4,40,18,20,2")
    args=ap.parse_args()

    eng=FpgaEngine(args.bit,args.dma)
    print("overlay loaded; DMA =",eng.dma_name)
    ts=np.load(args.testset)
    cfg=FxCfg(*[int(v) for v in args.fx.split(",")])
    W={n:pad_out(load_weights(os.path.join(args.models_dir,n,"weights.npz")),C.NOUT) for n in args.models}

    for mid,name in enumerate(args.models):
        sel=np.where(ts["model_id"]==mid)[0]
        X,y=ts["X"][sel],ts["y"][sel]
        nv=len(C.CLASSES[name])
        hw=np.stack([eng.infer(mid,x) for x in X])
        fx=forward_fixed(X,W[name],cfg)
        fl=forward_float(X,W[name])
        print(name,
              "HW acc",float((hw[:,:nv].argmax(1)==y).mean()),
              "fixed acc",float((fx[:,:nv].argmax(1)==y).mean()),
              "float acc",float((fl[:,:nv].argmax(1)==y).mean()),
              "bit-exact",int((hw==fx).all(axis=1).sum()),"/",len(y))

if __name__=="__main__":
    main()
