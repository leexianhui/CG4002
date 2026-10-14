"""Shared look for every figure: 1920x1080 PNG + SVG, type sized for video."""

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Reference palette (validated: 2-slot adjacent and 3-slot all-pairs, light surface).
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
NEUTRAL_FILL = "#f0efec"
SEQ_BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6",
            "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]

# Colour carries real vs fake; the individual label is always written on the axis.
CLASS_COLOR = {"real": BLUE, "fake": ORANGE}
# Weapons in firmware enum order (WEAPON_SWORD_SHIELD=1, SPEAR=2, MACE=3).
WEAPON_COLOR = {"sword_shield": BLUE, "spear": ORANGE, "mace": AQUA}
WEAPON_MARKER = {"sword_shield": "o", "spear": "s", "mace": "^"}
SLEEP_COLOR = {"off": BLUE, "on": ORANGE}

W_IN, H_IN, DPI = 16, 9, 120  # 1920 x 1080 px

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
    "axes.titlesize": 26, "axes.titleweight": "bold", "axes.titlelocation": "left", "axes.titlepad": 14,
    "axes.labelsize": 21, "axes.grid": True, "axes.axisbelow": True,
    "axes.spines.top": False, "axes.spines.right": False,
    "grid.color": GRID, "grid.linewidth": 1.0,
    "xtick.color": INK_2, "ytick.color": INK_2, "xtick.labelsize": 18, "ytick.labelsize": 18,
    "legend.frameon": False, "legend.fontsize": 18,
    "font.family": ["Segoe UI", "DejaVu Sans", "sans-serif"], "font.size": 18,
    "lines.linewidth": 2.2, "svg.fonttype": "none",
})


def new_fig(nrows=1, ncols=1, **kw):
    return plt.subplots(nrows, ncols, figsize=(W_IN, H_IN), dpi=DPI, **kw)


def footnote(fig, text):
    fig.text(0.012, 0.012, text, color=MUTED, fontsize=15, ha="left", va="bottom")


def simulation_banner(fig, text="SIMULATION — synthetic data, not sensor measurements"):
    fig.text(0.988, 0.985, text, ha="right", va="top", fontsize=19, fontweight="bold", color=SURFACE,
             bbox=dict(boxstyle="round,pad=0.4", facecolor=INK_2, edgecolor="none"))


def hthreshold(ax, y, label, *, side="right", color=INK_2, va="bottom"):
    """Horizontal dashed threshold with its name and value written on it."""
    ax.axhline(y, color=color, linestyle=(0, (6, 4)), linewidth=2, zorder=3)
    x = 0.995 if side == "right" else 0.005
    ax.text(x, y, " " + label + " ", transform=ax.get_yaxis_transform(), ha=side, va=va,
            color=color, fontsize=17, zorder=4, bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1, alpha=0.85))


def vthreshold(ax, x, label, *, color=INK_2, y=0.98, ha="left"):
    ax.axvline(x, color=color, linestyle=(0, (6, 4)), linewidth=2, zorder=3)
    ax.text(x, y, " " + label + " ", transform=ax.get_xaxis_transform(), ha=ha, va="top",
            color=color, fontsize=17, zorder=4, bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1, alpha=0.85))


def save(fig, out_dir, stem):
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for ext in ("png", "svg"):
        p = os.path.join(out_dir, f"{stem}.{ext}")
        fig.savefig(p, dpi=DPI)  # fixed canvas, no bbox_inches: exactly 1920x1080
        paths.append(p)
    plt.close(fig)
    return paths
