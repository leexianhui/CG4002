"""Analyse debug-listener CSVs (from listener.py) and produce labelled figures.

    python analyse.py logs/debug_..._sleep_on.csv logs/debug_..._sleep_off.csv

For each capture it reads the firmware's parse-friendly lines:
    [rtt] us=<n>                                   (hub, LATENCY_TEST builds)
    [beacon_iat] us=<n>,<n>,...                    (hub, LATENCY_TEST builds)
    [stats] to=<rx> from=<tx> type=<T> rx=<n> lost=<n>   (every board, 1/s)
    [cfg] wifi_sleep=on|off ...                    (every board, at boot)

and writes to --outdir (default plots/):
    rtt_histogram.png   RTT histogram, one colour per capture
    beacon_iat.png      TorsoBeacon inter-arrival time over the capture
    packet_loss.png     per-link packet loss table (also loss_table.csv)
"""

import argparse
import csv
import os
import re
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RTT_RE = re.compile(r"\[rtt\] us=(\d+)")
IAT_RE = re.compile(r"\[beacon_iat\] us=([\d,]+)")
STATS_RE = re.compile(r"\[stats\] to=(\S+) from=(\S+) type=(\S+) rx=(\d+) lost=(\d+)")
CFG_RE = re.compile(r"\[cfg\] wifi_sleep=(on|off)")

# Categorical slots 1-2 of the reference palette (validated adjacent pair),
# then the rest in fixed order for extra captures.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
SURFACE = "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "axes.edgecolor": "#c3c2b7",
    "axes.labelcolor": INK_2,
    "axes.titlecolor": INK,
    "axes.titlesize": 15,
    "axes.titleweight": "bold",
    "axes.labelsize": 12,
    "axes.grid": True,
    "axes.axisbelow": True,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelsize": 11,
    "ytick.labelsize": 11,
    "legend.frameon": False,
    "legend.fontsize": 11,
    "font.family": ["Segoe UI", "DejaVu Sans", "sans-serif"],
})


def load(path):
    cap = {"path": path, "rtt_ms": [], "iat_ms": [], "stats": {}, "sleep": None}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            line = row["line"]
            if m := RTT_RE.search(line):
                cap["rtt_ms"].append(int(m.group(1)) / 1000.0)
            elif m := IAT_RE.search(line):
                cap["iat_ms"].extend(int(v) / 1000.0 for v in m.group(1).split(",") if v)
            elif m := STATS_RE.search(line):
                to, frm, typ, rx, lost = m.groups()
                acc = cap["stats"].setdefault((frm, to, typ), [0, 0])
                acc[0] += int(rx)
                acc[1] += int(lost)
            elif m := CFG_RE.search(line):
                cap["sleep"] = m.group(1)
    return cap


def label_for(cap, override):
    if override:
        return override
    if cap["sleep"]:
        return f"WiFi sleep {cap['sleep'].upper()}"
    return os.path.splitext(os.path.basename(cap["path"]))[0]


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]


def plot_rtt(caps, outdir):
    with_data = [c for c in caps if c["rtt_ms"]]
    if not with_data:
        print("No [rtt] lines found - was the hub/torso built with LATENCY_TEST=1?")
        return
    # Axis covers every capture's p99 so one slow outlier doesn't squash the plot.
    xmax = max(pct(c["rtt_ms"], 99) for c in with_data) * 1.15
    bins = [xmax * i / 60 for i in range(61)]

    fig, ax = plt.subplots(figsize=(11, 6))
    for c in with_data:
        med = statistics.median(c["rtt_ms"])
        p95 = pct(c["rtt_ms"], 95)
        beyond = sum(v > xmax for v in c["rtt_ms"])
        extra = f", {beyond} beyond axis" if beyond else ""
        ax.hist(c["rtt_ms"], bins=bins, color=c["color"], alpha=0.55, edgecolor=SURFACE, linewidth=1,
                label=f"{c['label']}: median {med:.1f} ms, p95 {p95:.1f} ms (n={len(c['rtt_ms'])}{extra})")
        ax.axvline(med, color=c["color"], linewidth=2, linestyle="--")
    ax.set_xlim(0, xmax)
    ax.set_title("Hub ↔ torso round-trip time (PING/PONG, UDP broadcast)")
    ax.set_xlabel("Round-trip time (ms)")
    ax.set_ylabel("Number of pings")
    ax.legend(loc="upper right")
    fig.text(0.01, 0.01, "Dashed line = median. One PING every 100 ms; RTT measured on the hub's own clock.",
             color=MUTED, fontsize=10)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    path = os.path.join(outdir, "rtt_histogram.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"wrote {path}")


def plot_iat(caps, outdir):
    with_data = [c for c in caps if c["iat_ms"]]
    if not with_data:
        print("No [beacon_iat] lines found - beacons only flow after the weapon lock, in LATENCY_TEST builds.")
        return
    ymax = max(pct(c["iat_ms"], 99.5) for c in with_data) * 1.15
    ymax = max(ymax, 45)

    fig, axes = plt.subplots(len(with_data), 1, figsize=(11, 3.2 * len(with_data) + 1.2),
                             sharex=True, sharey=True, squeeze=False)
    for ax, c in zip(axes[:, 0], with_data):
        t, times = 0.0, []
        for v in c["iat_ms"]:
            t += v / 1000.0
            times.append(t)
        med = statistics.median(c["iat_ms"])
        p99 = pct(c["iat_ms"], 99)
        beyond = sum(v > ymax for v in c["iat_ms"])
        ax.scatter(times, [min(v, ymax) for v in c["iat_ms"]], s=9, color=c["color"], linewidths=0)
        ax.axhline(20, color=INK_2, linewidth=1.2, linestyle="--")
        ax.text(1.005, 20, "expected\n20 ms", transform=ax.get_yaxis_transform(), ha="left", va="center",
                color=INK_2, fontsize=10, clip_on=False)
        extra = f", {beyond} clipped at top" if beyond else ""
        ax.set_title(f"{c['label']}: median {med:.1f} ms, p99 {p99:.1f} ms (n={len(c['iat_ms'])}{extra})",
                     fontsize=12, loc="left")
        ax.set_ylim(0, ymax)
        ax.set_ylabel("Inter-arrival (ms)")
    axes[-1, 0].set_xlabel("Time since first beacon (s)")
    fig.suptitle("TorsoBeacon inter-arrival time at the hub (sent every 20 ms)",
                 fontsize=15, fontweight="bold", color=INK)
    fig.tight_layout()
    path = os.path.join(outdir, "beacon_iat.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"wrote {path}")


def loss_table(caps, outdir):
    rows = []
    for c in caps:
        for (frm, to, typ), (rx, lost) in sorted(c["stats"].items()):
            total = rx + lost
            rows.append([c["label"], f"{frm} → {to}", typ, rx, lost,
                         f"{100.0 * lost / total:.2f}%" if total else "-"])
    if not rows:
        print("No [stats] lines found.")
        return
    header = ["Capture", "Link (sender → receiver)", "Packet type", "Received", "Lost", "Loss"]

    csv_path = os.path.join(outdir, "loss_table.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

    # Console copy uses "->" — the Windows console codepage has no arrow.
    plain = [[str(v).replace("→", "->") for v in r] for r in [header] + rows]
    widths = [max(len(r[i]) for r in plain) for i in range(len(header))]
    print()
    for r in plain:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))
    print()

    fig, ax = plt.subplots(figsize=(11, 0.4 * (len(rows) + 1) + 0.6))
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=header, loc="center", cellLoc="left", colLoc="left",
                     colWidths=[0.2, 0.3, 0.15, 0.12, 0.1, 0.1])
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 1.6)
    for (r, col), cell in table.get_celld().items():
        cell.set_edgecolor(GRID)
        cell.set_facecolor(SURFACE)
        cell.get_text().set_color(INK if r else INK_2)
        if r == 0:
            cell.get_text().set_fontweight("bold")
        if col >= 3:
            cell.get_text().set_horizontalalignment("right")
            cell._loc = "right"
    ax.set_title("Packet loss per link (from sequence-number gaps)", pad=12)
    path = os.path.join(outdir, "packet_loss.png")
    fig.savefig(path, dpi=150, bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    print(f"wrote {path} and {csv_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+", help="one or more CSVs written by listener.py")
    ap.add_argument("--labels", nargs="+", help="legend label per CSV (default: from the [cfg] line)")
    ap.add_argument("--outdir", default="plots")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    caps = []
    for i, path in enumerate(args.csv):
        cap = load(path)
        cap["label"] = label_for(cap, args.labels[i] if args.labels and i < len(args.labels) else None)
        cap["color"] = SERIES[i % len(SERIES)]
        caps.append(cap)

    plot_rtt(caps, args.outdir)
    plot_iat(caps, args.outdir)
    loss_table(caps, args.outdir)


if __name__ == "__main__":
    main()
