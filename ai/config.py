"""
Single source of truth for the CG4002 AI subsystem.
Change class lists / window size HERE ONLY. Everything else (data generator,
training, HLS export, board server) imports from this file.

Values below follow the Group-23 design report (Sections 4.5, 5.2). The class
lists follow the Section 5.2.2 table (7 classes per weapon incl. IDLE). If your
team changes the class list, edit ATTACK_CLASSES and re-run the pipeline.
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

# ---- signal / window -------------------------------------------------------
FS = 50                 # Hz, wrist IMU uplink rate (report 3.2.3)
WINDOW = 100             # samples per classification window (1.0 s @ 50 Hz)  <- tune with real data
N_CH = 6                # ax ay az gx gy gz
STRIDE = 10              # samples between windows (100 ms -> the ~10 Hz of report 4.5)
CHANNELS = ["ax", "ay", "az", "gx", "gy", "gz"]   # accel in g, gyro in deg/s

# ---- classes ----------------------------------------------------------------
WEAPONS = ["SWORD", "SPEAR", "MACE"]
MOVE_CLASSES = ["IDLE", "HORIZONTAL_SWING", "VERTICAL_SWING", "DIAGONAL_SWING", "THRUST", "SPIN_ATTACK", "UPPERCUT"]
GUARD_CLASSES = ["IDLE", "GUARDING"]
ATTACK_CLASSES = {w: list(MOVE_CLASSES) for w in WEAPONS} 

MODEL_NAMES = WEAPONS + ["GUARD"]                    # the 4 models of the report
MODEL_IDS = {n: i for i, n in enumerate(MODEL_NAMES)}  # index used by the FPGA IP (model_id)
CLASSES = dict(ATTACK_CLASSES)
CLASSES["GUARD"] = GUARD_CLASSES

# Union of all attack classes (used by design "B": one shared attack model)
UNION_CLASSES = []
for _w in WEAPONS:
    for _c in ATTACK_CLASSES[_w]:
        if _c not in UNION_CLASSES:
            UNION_CLASSES.append(_c)

# Hardware output width: every model is padded to this many logits so that ONE
# IP core can serve all four models (design "C"). PS uses only the first
# len(CLASSES[model]) logits.
NOUT = max(len(v) for v in CLASSES.values())

# ---- network (report 5.2.4) -------------------------------------------------
ARCH = dict(kernel=5, f1=16, f2=32, dense=32)

# ---- misc ---------------------------------------------------------------------
PERSONS = ["P1", "P2", "P3", "P4"]
SEED = 42

DATA_DIR = os.path.join(ROOT, "data", "synthetic")
MODELS_DIR = os.path.join(ROOT, "models")
RESULTS_DIR = os.path.join(ROOT, "results")
