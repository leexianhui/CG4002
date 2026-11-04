"""
Final Ultra96 classifier service: ACTION_FINAL (model_id 0 on the FPGA IP). Guard is detected by
the hardware, not here, so the GUARD_FINAL model is no longer used.

Line-delimited JSON over TCP 127.0.0.1:8888. The relay reaches it through the SSH tunnel.
Comms boundary = shared/protocol/app_messages.md 3.1/3.2:

  in : {"type":"SENSOR_WINDOW","version":1,"playerId":1,"requestId":42,"weapon":"SWORD",
        "timestamp":12345678,"windowStartTs":..,"windowEndTs":..,"sampleRateHz":50,"stalePct":0,
        "dominant":[[ax,ay,az,gx,gy,gz],...],      right wrist  } 100 rows each at 50 Hz (2 s),
        "offhand":[[...],...],                     left wrist   } in g and deg/s, oldest first:
        "torso":[[...],...]}                       torso        } one (100, 18) model input
       Also accepted (AI team testing): "samples": 100 rows x 18 = RIGHT, LEFT, TORSO.
  out: {"type":"ACTION_RESULT","version":1,"playerId":1,"requestId":42,"timestamp":12345678,
        "action":"HORIZONTAL_SWING","actionConfidence":0.94,
        "guardState":"IDLE","guardConfidence":0.0, ...extra AI fields}

Boundary rules (Comms, 2026-11-04):
  * requestId, playerId and timestamp are echoed UNCHANGED: the relay matches replies by
    requestId and drops a reply whose timestamp/playerId differ.
  * A sensor that sent nothing (torso null), or a perfectly constant block (the relay's off-hand
    placeholder when the left wrist was not measured), is filled with the model's training mean,
    i.e. 0 after normalisation, instead of being fed as fake motion.
  * guardState is always IDLE with guardConfidence 0: guard comes from the hardware.
  * Action classes the game has no rules for (COMMS_ACTION_MAP) are reported as IDLE; the AI label
    stays in "rawAction".

Backends:
  python3 classifier_server.py --backend stub                     random answers, no model
  python3 classifier_server.py --backend numpy                    float CNN on the ARM cores
  python3 classifier_server.py --backend fixed                    bit-accurate copy of the FPGA maths
  sudo -E python3 classifier_server.py --backend fpga --bit cnn.bit   the FPGA
"""
import argparse, asyncio, json, os, random, sys, time
import numpy as np

HERE=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,HERE)
sys.path.insert(0,os.path.join(HERE,".."))
sys.path.insert(0,os.path.join(HERE,"..","src"))
import config as C
from data_utils_lite import fit_window, load_norm_json
from fixedpoint_ref import FxCfg, forward_fixed, forward_float, load_weights, pad_out, softmax

PROTOCOL_VERSION=1

ACTION_MODEL="ACTION_FINAL"
MODELS=[ACTION_MODEL]   # GUARD_FINAL retired: guard is detected by the hardware

ATTACK_THRESHOLD=0.70

# Action names the comms engine accepts (comms ActionClass / shared/schemas/action_result.schema.json).
COMMS_ACTIONS={"IDLE","HORIZONTAL_SWING","VERTICAL_SWING","DIAGONAL_SWING","THRUST",
               "CHARGED_ATTACK","CHARGED_THRUST","SWING"}
# AI classes with no game rule yet -> what the engine is told. To make one count, map it to a game
# action here (or add the move to shared/config/weapons.yaml and COMMS_ACTIONS).
COMMS_ACTION_MAP={"SPIN_ATTACK":"IDLE","UPPERCUT":"IDLE"}

# 18-channel layout of config.CHANNELS: which request field fills which columns.
SENSOR_BLOCKS=(("dominant",0),("offhand",6),("torso",12))

class StubBackend(object):
    def logits(self,model,x_raw):
        n=len(C.CLASSES[model])
        z=np.full(n,-2.0); z[random.randrange(n)]=3.0
        return z

class NumpyBackend(object):
    def __init__(self,models_dir,fixed=False,fx=None):
        self.W={n:load_weights(os.path.join(models_dir,n,"weights.npz")) for n in MODELS}
        self.norm={n:load_norm_json(os.path.join(models_dir,n,"norm.json")) for n in MODELS}
        self.fixed=fixed
        self.cfg=FxCfg(*fx) if fx else FxCfg()
        if fixed:
            self.W={n:pad_out(w,C.NOUT) for n,w in self.W.items()}

    def normalise(self,model,x_raw,missing):
        m,s=self.norm[model]
        x=((x_raw-m)/s).astype(np.float32)
        for start in missing:
            x[:,start:start+6]=0.0      # absent sensor = training mean
        return x

    def logits(self,model,x_raw,missing=()):
        x=self.normalise(model,x_raw,missing)
        nv=len(C.CLASSES[model])
        z=forward_fixed(x,self.W[model],self.cfg) if self.fixed else forward_float(x,self.W[model])
        return np.asarray(z)[:nv]

class FpgaBackend(NumpyBackend):
    def __init__(self,models_dir,bit):
        NumpyBackend.__init__(self,models_dir)
        from fpga_infer import FpgaEngine
        self.eng=FpgaEngine(bit)

    def logits(self,model,x_raw,missing=()):
        x=self.normalise(model,x_raw,missing)
        return self.eng.infer(C.MODEL_IDS[model],x)[:len(C.CLASSES[model])]

def build_window(req):
    """-> (WINDOW, 18) raw window, and the start columns of sensors that carry no data."""
    if "samples" in req:
        x=fit_window(req["samples"],C.WINDOW)
        if x.shape[1]!=C.N_CH:
            raise ValueError("samples must have %d channels, got %d"%(C.N_CH,x.shape[1]))
        return x,()
    x=np.zeros((C.WINDOW,C.N_CH),np.float32)
    missing=[]
    for field,start in SENSOR_BLOCKS:
        rows=req.get(field)
        if not rows:
            missing.append(start); continue
        block=fit_window(rows,C.WINDOW)
        if block.shape[1]!=6:
            raise ValueError("%s rows must have 6 values, got %d"%(field,block.shape[1]))
        x[:,start:start+6]=block
        if np.all(block==block[0]):     # constant = placeholder, not a measurement
            missing.append(start)
    return x,tuple(missing)

def probs_for(backend,model,x,missing):
    if isinstance(backend,StubBackend):
        z=backend.logits(model,x)
    else:
        z=backend.logits(model,x,missing)
    return softmax(np.asarray(z,dtype=np.float64))

def handle_request(backend,req,debug=False):
    if req.get("type")=="PING":
        return {"type":"PONG","timestamp":int(time.time()*1000)}
    if req.get("type")!="SENSOR_WINDOW":
        return {"type":"ERROR","error":"unknown type %r"%req.get("type")}
    for key in ("timestamp","playerId","requestId"):
        if key not in req:
            return {"type":"ERROR","error":"missing %s"%key,"requestId":req.get("requestId")}
    if "samples" not in req and "dominant" not in req:
        return {"type":"ERROR","error":"missing dominant","requestId":req["requestId"]}

    weapon=str(req.get("weapon","")).upper()
    if weapon not in C.WEAPONS:
        return {"type":"ERROR","error":"unknown weapon %r"%weapon,"requestId":req["requestId"]}

    t0=time.perf_counter()
    x,missing=build_window(req)

    ap=probs_for(backend,ACTION_MODEL,x,missing)
    ai=int(ap.argmax())
    raw_action=C.CLASSES[ACTION_MODEL][ai]
    a_conf=float(ap[ai])

    action=raw_action
    if raw_action!="IDLE" and a_conf<ATTACK_THRESHOLD:
        action="IDLE"

    comms_action=COMMS_ACTION_MAP.get(action,action)
    if comms_action not in COMMS_ACTIONS:
        comms_action="IDLE"

    out={
        "type":"ACTION_RESULT",
        "version":PROTOCOL_VERSION,
        "playerId":req["playerId"],
        "requestId":req["requestId"],
        "timestamp":req["timestamp"],
        "weapon":weapon,
        "action":comms_action,
        "rawAction":raw_action,
        "actionConfidence":round(a_conf,4),
        "guardState":"IDLE",
        "guardConfidence":0.0,
    }
    if debug:
        out["latencyMs"]=round((time.perf_counter()-t0)*1000,3)
        out["missingSensors"]=[f for f,s in SENSOR_BLOCKS if s in missing]
    return out

def make_server(backend,debug):
    async def client(reader,writer):
        peer=writer.get_extra_info("peername")
        print("client connected:",peer)
        try:
            while True:
                line=await reader.readline()
                if not line: break
                try:
                    resp=handle_request(backend,json.loads(line.decode()),debug)
                except Exception as e:  # one bad packet must not kill the service
                    resp={"type":"ERROR","error":"%s: %s"%(type(e).__name__,e)}
                writer.write((json.dumps(resp)+"\n").encode())
                await writer.drain()
        finally:
            writer.close()
            print("client disconnected:",peer)
    return client

async def serve(host,port,backend,debug):
    await asyncio.start_server(make_server(backend,debug),host,port)
    print("classifier service listening on %s:%d backend=%s"%(host,port,type(backend).__name__))
    await asyncio.Future()

def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend",choices=["stub","numpy","fixed","fpga"],default="stub")
    ap.add_argument("--host",default="127.0.0.1")
    ap.add_argument("--port",type=int,default=8888)
    ap.add_argument("--bit",default=None)
    ap.add_argument("--models-dir",default=C.MODELS_DIR)
    ap.add_argument("--debug",action="store_true",help="add latencyMs and missingSensors to replies")
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
