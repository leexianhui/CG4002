"""
Final Ultra96 classifier service.

Input JSON:
{
  "type":"SENSOR_WINDOW",
  "playerId":1,
  "weapon":"SWORD",
  "timestamp":12345678,
  "samples":[ [18 floats], ... exactly 100 rows ... ]
}

Channel order:
RIGHT ax ay az gx gy gz,
LEFT  ax ay az gx gy gz,
TORSO ax ay az gx gy gz

Output includes raw action, guard state, and finalAction.
Guard has priority over attack.
"""
import argparse, asyncio, json, os, random, sys, time
import numpy as np

HERE=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,os.path.join(HERE,".."))
sys.path.insert(0,os.path.join(HERE,"..","src"))
import config as C
from fixedpoint_ref import FxCfg, forward_fixed, forward_float, load_weights, pad_out, softmax

ACTION_MODEL="ACTION_FINAL"
GUARD_MODEL="GUARD_FINAL"

ATTACK_THRESHOLD=0.70
GUARD_ENTER=0.55
GUARD_EXIT=0.35
GUARD_ENTER_COUNT=2
GUARD_EXIT_COUNT=3

_guard_state={}

def load_norm(path):
    d=json.load(open(path))
    return np.asarray(d["mean"],np.float32),np.asarray(d["std"],np.float32)

class StubBackend(object):
    def logits(self,model,x_raw):
        n=len(C.CLASSES[model])
        z=np.full(n,-2.0); z[random.randrange(n)]=3.0
        return z

class NumpyBackend(object):
    def __init__(self,models_dir,fixed=False,fx=None):
        self.W={n:load_weights(os.path.join(models_dir,n,"weights.npz")) for n in C.MODEL_NAMES}
        self.norm={n:load_norm(os.path.join(models_dir,n,"norm.json")) for n in C.MODEL_NAMES}
        self.fixed=fixed
        self.cfg=FxCfg(*fx) if fx else FxCfg()
        if fixed:
            self.W={n:pad_out(w,C.NOUT) for n,w in self.W.items()}

    def logits(self,model,x_raw):
        m,s=self.norm[model]
        x=((x_raw-m)/s).astype(np.float32)
        if model==GUARD_MODEL:
            x[:,12:]=0.0
        nv=len(C.CLASSES[model])
        z=forward_fixed(x,self.W[model],self.cfg) if self.fixed else forward_float(x,self.W[model])
        return np.asarray(z)[:nv]

class FpgaBackend(NumpyBackend):
    def __init__(self,models_dir,bit):
        NumpyBackend.__init__(self,models_dir)
        from fpga_infer import FpgaEngine
        self.eng=FpgaEngine(bit)

    def logits(self,model,x_raw):
        m,s=self.norm[model]
        x=((x_raw-m)/s).astype(np.float32)
        if model==GUARD_MODEL:
            x[:,12:]=0.0
        return self.eng.infer(C.MODEL_IDS[model],x)[:len(C.CLASSES[model])]

def validate_samples(req):
    x=np.asarray(req.get("samples"),dtype=np.float32)
    if x.shape != (C.WINDOW,C.N_CH):
        raise ValueError("samples must have shape (%d,%d), got %r" % (C.WINDOW,C.N_CH,x.shape))
    return x

def probs_for(backend,model,x):
    z=np.asarray(backend.logits(model,x),dtype=np.float64)
    return softmax(z)

def update_guard(player_id,guard_prob):
    key=str(player_id)
    st=_guard_state.setdefault(key,{"active":False,"enter":0,"exit":0})
    if not st["active"]:
        if guard_prob>=GUARD_ENTER:
            st["enter"]+=1
        else:
            st["enter"]=0
        if st["enter"]>=GUARD_ENTER_COUNT:
            st["active"]=True
            st["enter"]=0
            st["exit"]=0
    else:
        if guard_prob<=GUARD_EXIT:
            st["exit"]+=1
        else:
            st["exit"]=0
        if st["exit"]>=GUARD_EXIT_COUNT:
            st["active"]=False
            st["enter"]=0
            st["exit"]=0
    return st["active"]

def handle_request(backend,req,debug=False):
    if req.get("type")=="PING":
        return {"type":"PONG","timestamp":int(time.time()*1000)}
    if req.get("type")!="SENSOR_WINDOW":
        return {"type":"ERROR","error":"unknown type %r"%req.get("type")}

    weapon=str(req.get("weapon","")).upper()
    if weapon and weapon not in C.WEAPONS:
        return {"type":"ERROR","error":"unknown weapon %r"%weapon}

    t0=time.perf_counter()
    x=validate_samples(req)

    ap=probs_for(backend,ACTION_MODEL,x)
    ai=int(ap.argmax())
    raw_action=C.CLASSES[ACTION_MODEL][ai]
    a_conf=float(ap[ai])

    action=raw_action
    if raw_action!="IDLE" and a_conf<ATTACK_THRESHOLD:
        action="IDLE"

    gp=probs_for(backend,GUARD_MODEL,x)
    guard_prob=float(gp[1])  # GUARDING is class index 1
    guard_active=update_guard(req.get("playerId"),guard_prob)

    final_action="GUARD" if guard_active else action

    out={
        "type":"ACTION_RESULT",
        "playerId":req.get("playerId"),
        "weapon":weapon,
        "action":action,
        "rawAction":raw_action,
        "actionConfidence":round(a_conf,4),
        "guardState":"GUARDING" if guard_active else "NOT_GUARDING",
        "guardConfidence":round(guard_prob,4),
        "finalAction":final_action,
        "timestamp":int(time.time()*1000),
    }
    if debug:
        out["latencyMs"]=round((time.perf_counter()-t0)*1000,3)
    return out

def make_server(backend,debug):
    async def client(reader,writer):
        print("client connected:",writer.get_extra_info("peername"))
        try:
            while True:
                line=await reader.readline()
                if not line: break
                try:
                    resp=handle_request(backend,json.loads(line.decode()),debug)
                except Exception as e:
                    resp={"type":"ERROR","error":"%s: %s"%(type(e).__name__,e)}
                writer.write((json.dumps(resp)+"\n").encode())
                await writer.drain()
        finally:
            writer.close()
            print("client disconnected")
    return client

async def serve(host,port,backend,debug):
    await asyncio.start_server(make_server(backend,debug),host,port)
    print("classifier service listening on %s:%d backend=%s"%(host,port,type(backend).__name__))
    await asyncio.Future()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--backend",choices=["stub","numpy","fixed","fpga"],default="stub")
    ap.add_argument("--host",default="127.0.0.1")
    ap.add_argument("--port",type=int,default=8888)
    ap.add_argument("--bit",default=None)
    ap.add_argument("--models-dir",default=C.MODELS_DIR)
    ap.add_argument("--debug",action="store_true")
    a=ap.parse_args()

    if a.backend=="stub":
        be=StubBackend()
    elif a.backend in ("numpy","fixed"):
        be=NumpyBackend(a.models_dir,fixed=(a.backend=="fixed"))
    else:
        if not a.bit: sys.exit("--bit required for fpga backend")
        be=FpgaBackend(a.models_dir,a.bit)

    try:
        if hasattr(asyncio,"run"):
            asyncio.run(serve(a.host,a.port,be,a.debug))
        else:
            asyncio.get_event_loop().run_until_complete(serve(a.host,a.port,be,a.debug))
    except KeyboardInterrupt:
        print("bye")

if __name__=="__main__":
    main()
