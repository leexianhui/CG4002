"""
CG4002 final AI configuration.

One FPGA IP serves two CNNs:
  model_id 0 = ACTION_FINAL
  model_id 1 = GUARD_FINAL

Both use input shape (100, 18).
GUARD physically uses RIGHT+LEFT only; torso channels are zeroed before guard inference.
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

FS = 50
WINDOW = 100
N_CH = 18
STRIDE = 10

CHANNELS = [
    "right_ax", "right_ay", "right_az", "right_gx", "right_gy", "right_gz",
    "left_ax", "left_ay", "left_az", "left_gx", "left_gy", "left_gz",
    "torso_ax", "torso_ay", "torso_az", "torso_gx", "torso_gy", "torso_gz",
]

GYRO_SLICES = [(3, 6), (9, 12), (15, 18)]

ACTION_CLASSES = [
    "IDLE",
    "HORIZONTAL_SWING",
    "VERTICAL_SWING",
    "DIAGONAL_SWING",
    "THRUST",
    "SPIN_ATTACK",
    "UPPERCUT",
]

GUARD_CLASSES = ["NOT_GUARDING", "GUARDING"]
WEAPONS = ["SWORD", "SPEAR", "MACE"]

MODEL_NAMES = ["ACTION_FINAL", "GUARD_FINAL"]
MODEL_IDS = {"ACTION_FINAL": 0, "GUARD_FINAL": 1}

CLASSES = {
    "ACTION_FINAL": list(ACTION_CLASSES),
    "GUARD_FINAL": list(GUARD_CLASSES),
}

NOUT = 7
ARCH = dict(kernel=5, f1=16, f2=32, dense=32)

PERSONS = ["P1", "P2", "P3", "P4"]
SEED = 42

DATA_DIR = os.path.join(ROOT, "data", "real_3imu")
MODELS_DIR = os.path.join(ROOT, "models")
RESULTS_DIR = os.path.join(ROOT, "results")
