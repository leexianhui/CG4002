"""Figure I: four diagrams drawn with matplotlib (no graphviz needed). Every
number on them — ports, packet sizes, intervals, thresholds — is read from
the firmware, so they stay correct after a retune.
"""

import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

from . import style as S
from .figures import FigResult

NODE_FILL, NODE_EDGE = "#e4effc", S.BLUE
STATE_FILL = "#eef6ee"
REJECT_FILL, REJECT_EDGE = "#fde9e1", S.ORANGE
LAPTOP_FILL = S.NEUTRAL_FILL


def _canvas():
    fig, ax = S.new_fig()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 9)
    ax.axis("off")
    return fig, ax


def _box(ax, cx, cy, w, h, text, *, fc=NODE_FILL, ec=NODE_EDGE, fs=17, weight="normal", color=None):
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h, boxstyle="round,pad=0.02,rounding_size=0.15",
                                fc=fc, ec=ec, lw=2.2, zorder=2))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=fs, fontweight=weight, color=color or S.INK,
            zorder=3, linespacing=1.35)


def _arrow(ax, p, q, *, color=S.INK_2, rad=0.0, ls="-", lw=2.2, both=False):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle="<|-|>" if both else "-|>", mutation_scale=22, color=color,
                                 lw=lw, linestyle=ls, connectionstyle=f"arc3,rad={rad}", zorder=1,
                                 shrinkA=2, shrinkB=2))


def _title(ax, text, sub=None):
    ax.text(0.2, 8.7, text, fontsize=28, fontweight="bold", color=S.INK, va="top")
    if sub:
        ax.text(0.2, 8.15, sub, fontsize=17, color=S.INK_2, va="top")


def _result(stem, title, fw, files, takeaway):
    r = FigResult("I", stem, title, status="made", files=files,
                  sources=[p.replace("\\", "/") for p in fw.paths.values()], counts={}, takeaway=takeaway)
    r.caveats = ["Diagram; numbers are read from the firmware at build time."]
    return r


def diagram_architecture(fw, out):
    rw, lw, to = "right_wrist", "left_wrist", "torso"
    sz = fw.sizes
    pose, link = fw.rw("POSE_PORT"), fw.rw("LINK_PORT")
    dbg, resp = fw.rw("DEBUG_PORT"), fw.rw("RESULT_PORT")
    hub_ms = fw.rw("HUB_STATUS_INTERVAL_MS")
    lw_ms, to_ms = fw.c(lw, "POSE_BROADCAST_INTERVAL_MS"), fw.c(to, "POSE_BROADCAST_INTERVAL_MS")
    bcn = fw.c(to, "BEACON_INTERVAL_MS")
    hz = 1e6 / fw.rw("SAMPLE_PERIOD_US")

    fig, ax = _canvas()
    _title(ax, "System architecture", f"Three ESP32 + MPU-6500 boards on one WiFi hotspot, UDP broadcast, "
                                      f"IMUs sampled at {hz:.0f} Hz")
    y = 5.2
    _box(ax, 1.75, y, 3.1, 2.0, "LEFT WRIST\npose (el, az)\n+ stillness", fs=17)
    _box(ax, 8.0, y, 3.2, 2.0, "RIGHT WRIST — HUB\nweapon detection\nsegmentation + gates", fs=17,
         weight="bold")
    _box(ax, 14.25, y, 3.1, 2.0, f"TORSO\npose, then {1000 / bcn:.0f} Hz\ngyro beacons", fs=17)
    _box(ax, 8.0, 1.05, 7.5, 1.1, "LAPTOP   listener.py (debug)   ·   result_listener.py (milestones)",
         fc=LAPTOP_FILL, ec=S.AXIS, fs=17)

    # left <-> hub
    _arrow(ax, (3.3, y + 0.35), (6.4, y + 0.35))
    ax.text(4.85, y + 0.5, f"WristPose {sz['WristPose']} B\n:{pose} · every {lw_ms} ms", ha="center",
            va="bottom", fontsize=14.5, color=S.INK)
    _arrow(ax, (6.4, y - 0.35), (3.3, y - 0.35))
    ax.text(4.85, y - 0.5, f"HubStatus {sz['HubStatus']} B\n:{pose} · every {hub_ms} ms", ha="center",
            va="top", fontsize=14.5, color=S.INK)
    # torso <-> hub
    _arrow(ax, (12.7, y + 0.35), (9.6, y + 0.35))
    ax.text(11.15, y + 0.5, f"TorsoPose {sz['TorsoPose']} B · :{pose} · {to_ms} ms\n(before lock)\n"
                            f"TorsoBeacon {sz['TorsoBeacon']} B · :{link} · {bcn} ms\n(after lock)\n"
                            f"SegmentAck {sz['SegmentAck']} B · :{link}",
            ha="center", va="bottom", fontsize=14.5, color=S.INK)
    _arrow(ax, (9.6, y - 0.35), (12.7, y - 0.35))
    ax.text(11.15, y - 0.5, f"HubStatus {sz['HubStatus']} B · :{pose} · {hub_ms} ms\n"
                            f"SegmentRequest {sz['SegmentRequest']} B · :{link}\n(per accepted swing,\nretried until acked)",
            ha="center", va="top", fontsize=14.5, color=S.INK)
    # latency probe
    ax.text(11.15, y - 1.62, f"+ PING / PONG {sz['LatencyProbe']} B · :{link}\n(LATENCY_TEST builds only)",
            ha="center", va="top", fontsize=13.5, color=S.MUTED)
    # to laptop
    for x0 in (1.75, 14.25):
        _arrow(ax, (x0, y - 1.05), (x0 * 0.35 + 8.0 * 0.65, 1.62), ls=(0, (5, 4)), color=S.MUTED)
    _arrow(ax, (7.6, y - 1.05), (7.6, 1.62), ls=(0, (5, 4)), color=S.MUTED)
    _arrow(ax, (8.4, y - 1.05), (8.4, 1.62), color=S.INK_2)
    ax.text(7.45, 2.25, f"debug text :{dbg}\n(all boards)", ha="right", fontsize=14.5, color=S.INK_2)
    ax.text(8.55, 2.25, f"milestones :{resp}\n(hub only)", ha="left", fontsize=14.5, color=S.INK_2)
    files = S.save(fig, out, "09a_diagram_architecture")
    return _result("09a_diagram_architecture", "System architecture", fw, files,
                   "The hub is the only decision-maker; the other two boards stream pose, then torso beacons.")


def diagram_phases(fw, out):
    hold = fw.rw("WEAPON_LOCK_HOLD_MS") / 1000
    grace = fw.rw("WEAPON_DROPOUT_GRACE_MS")
    match = fw.rw("WEAPON_MATCH_MAX_DEG")
    fresh = fw.rw("POSE_FRESHNESS_MS")
    still = fw.rw("STILLNESS_GYRO_THRESHOLD_DPS")
    still_ms = fw.rw("STILLNESS_HOLD_MS")
    fig, ax = _canvas()
    _title(ax, "Hub game phases", "HubStatus carries the phase to the other boards every "
                                  f"{fw.rw('HUB_STATUS_INTERVAL_MS')} ms")
    y = 5.0
    _box(ax, 2.4, y, 3.2, 1.3, "WAITING", fc=STATE_FILL, ec=S.INK_2, fs=24, weight="bold")
    _box(ax, 8.0, y, 3.2, 1.3, "DETECTING", fc=STATE_FILL, ec=S.INK_2, fs=24, weight="bold")
    _box(ax, 13.6, y, 3.2, 1.3, "LOCKED", fc=STATE_FILL, ec=S.INK_2, fs=24, weight="bold")
    _arrow(ax, (0.15, y), (0.8, y))
    ax.text(0.2, y + 0.2, "boot", fontsize=15, color=S.INK_2)
    _arrow(ax, (4.0, y), (6.4, y))
    ax.text(5.2, y + 0.2, f"left + torso poses\nfresh (< {fresh} ms)", ha="center", va="bottom", fontsize=15)
    ax.text(5.2, y - 0.2, "all_nodes_ready", ha="center", va="top", fontsize=15, color=S.BLUE,
            fontweight="bold")
    _arrow(ax, (9.6, y), (12.0, y))
    ax.text(10.8, y + 0.2, f"same weapon\nheld ≥ {hold:g} s", ha="center", va="bottom", fontsize=15)
    ax.text(10.8, y - 0.2, "weapon_locked=X", ha="center", va="top", fontsize=15, color=S.BLUE,
            fontweight="bold")
    _arrow(ax, (7.2, y + 0.66), (8.8, y + 0.66), rad=-1.6)
    ax.text(8.0, y + 2.05, f"unknown / other / unstable for > {grace} ms\n→ weapon_candidate_lost, "
                           "hold timer restarts", ha="center", fontsize=15, color=S.INK_2)
    ax.text(2.4, y - 1.0, "calibrated;\nwaiting for both\nother boards", ha="center", va="top", fontsize=15,
            color=S.INK_2)
    margin = fw.rw("WEAPON_MATCH_MARGIN_DEG")
    ax.text(8.0, y - 1.0, f"classify every tick:\nnearest weapon ≤ {match:g}° and\n≥ {margin:g}° closer than the next,\n"
                          "both wrists still "
                          f"(EMA < {still:g} dps\nfor {still_ms} ms), torso connected", ha="center", va="top", fontsize=15,
            color=S.INK_2)
    ax.text(13.6, y - 1.0, "valid-action algorithm;\ntorso switches to beacons;\nstays until reboot",
            ha="center", va="top", fontsize=15, color=S.INK_2)
    ax.text(0.2, 0.4, f"Dropouts shorter than WEAPON_DROPOUT_GRACE_MS ({grace} ms) are ridden through "
                      "without resetting the hold.", fontsize=15, color=S.MUTED)
    files = S.save(fig, out, "09b_diagram_phases")
    return _result("09b_diagram_phases", "Game phase machine", fw, files,
                   f"A weapon locks only after a continuous {hold:g} s hold; after that the board runs the "
                   "swing algorithm until reboot.")


def diagram_loop(fw, out):
    hz = 1e6 / fw.rw("SAMPLE_PERIOD_US")
    fs = fw.gyro_full_scale_dps()
    st, js = fw.rw("START_THRESHOLD"), fw.rw("JERK_START_THRESHOLD")
    settle, pre, post = fw.rw("SETTLE_FRACTION"), fw.rw("PRE_PEAK_MS"), fw.rw("POST_PEAK_MS")
    npre = fw.rw("PRETRIGGER_SAMPLES")
    fig, ax = _canvas()
    _title(ax, "Sensor → algorithm → actuator", "Runs on the hub once the weapon is locked")
    y = 5.3
    boxes = [
        (1.65, "IMU\nMPU-6500\n" f"{hz:.0f} Hz, ±{round(fs, -1):.0f} dps"),
        (5.0, "SEGMENT\n" f"open: > {st:g} dps\nor jerk > {js:g} g\n+ {npre}-sample pretrigger"),
        (8.35, "TRIM\n" f"−{pre} / +{post} ms\naround peak\nclose: < {settle:g}×peak\nfor {post} ms"),
        (11.7, "VALIDITY GATES\ntorso · duration\nshape · magnitude"),
    ]
    for cx, t in boxes:
        _box(ax, cx, y, 2.9, 2.3, t, fs=16)
    for a, b in zip(boxes, boxes[1:]):
        _arrow(ax, (a[0] + 1.48, y), (b[0] - 1.48, y))
    _box(ax, 11.7, 8.1 - 0.35, 2.9, 0.9, f"TORSO beacons\n{fw.c('torso', 'BEACON_INTERVAL_MS')} ms",
         fc=S.NEUTRAL_FILL, ec=S.AXIS, fs=15)
    _arrow(ax, (11.7, 7.3), (11.7, y + 1.18))
    _box(ax, 14.75, 6.35, 1.9, 1.3, "ACCEPT\nLED flash\n+ haptic", fs=15)
    _box(ax, 14.75, 4.25, 1.9, 1.3, "REJECT\nLED flash", fc=REJECT_FILL, ec=REJECT_EDGE, fs=15)
    _arrow(ax, (13.18, y + 0.35), (13.78, 6.35))
    _arrow(ax, (13.18, y - 0.35), (13.78, 4.25))
    ax.text(15.75, 7.12, "SegmentRequest → torso\naction_accepted → laptop", ha="right", va="bottom",
            fontsize=13.5, color=S.INK_2)
    # feedback loop: player reacts to LED/haptic, next swing comes from the IMU
    _arrow(ax, (14.75, 3.55), (1.65, y - 1.2), rad=-0.28, color=S.MUTED, ls=(0, (5, 4)))
    ax.text(8.2, 1.5, "player sees / feels the result and makes the next move", ha="center", fontsize=16,
            color=S.INK_2)
    files = S.save(fig, out, "09c_diagram_sensor_loop")
    return _result("09c_diagram_sensor_loop", "Sensor → algorithm → actuator loop", fw, files,
                   "Every swing goes IMU → segment → trim → gates → LED/haptic feedback.")


def diagram_gates(fw, out, gates_order, named):
    va = lambda n: fw.fn("isValidAttack", n)  # noqa: E731
    mk = lambda n: fw.fn("matchesKnownPattern", n)  # noqa: E731
    text = {
        "torso_data": "Torso beacon data present in the swing window",
        "torso_engagement": f"Torso engagement: torsoPeak ≥ {fw.rw('MIN_TORSO_ENGAGEMENT'):g} dps "
                            "(waived if spin-likely)",
        "torso_ceiling": f"Torso ceiling: torsoPeak < {fw.rw('TORSO_GATE_MAX_NORMAL'):g} dps "
                         f"(spin-likely: < {fw.rw('TORSO_GATE_MAX_SPIN'):g})",
        "raw_duration": f"Raw duration ≤ {fw.rw('RAW_MAX_DURATION_MS'):g} ms",
        "swing_shape": f"Swing shape: reversals ≤ {fw.rw('SWING_MAX_REVERSALS')} and sustain ≥ "
                       f"{fw.rw('SWING_MIN_SUSTAIN_FRACTION'):g}   (skipped if spin- or thrust-likely)",
        "spin_reversal": f"Spin: reversals ≤ {fw.rw('SPIN_MAX_REVERSALS')}   (only if raw rotation > {fw.rw('SPIN_RAW_ROTATION_DEG'):g}°)",
        "magnitude": f"Magnitude: {va('MIN_DURATION')}–{va('MAX_DURATION')} ms, ≥ {va('MIN_SAMPLES')} samples, "
                     f"peak ≥ {va('MIN_PEAK_GYRO_DPS'):g} dps and jerk ≥ {va('MIN_PEAK_JERK_G'):g} g",
        "pattern": f"Pattern: rotation > {mk('MIN_MEANINGFUL_ROTATION'):g}° or jerk > "
                   f"{mk('MIN_MEANINGFUL_JERK'):g} g",
    }
    fig, ax = _canvas()
    _title(ax, "Validity-gate pipeline", f"Thrust-likely = trimmed rotation < {fw.rw('THRUST_MAX_ROTATION_DEG'):g}°. "
                                         "Every gate must pass; a failure at any one rejects.")
    keys = [k for k, _ in gates_order]
    top, step = 7.35, 0.74
    _box(ax, 5.6, top, 10.4, 0.56, "Motion ENDED — features from the trimmed + raw window", fc=S.NEUTRAL_FILL,
         ec=S.AXIS, fs=16, weight="bold")
    ys = [top - step * (i + 1) for i in range(len(keys))]
    for k, y in zip(keys, ys):
        _box(ax, 5.6, y, 10.4, 0.56, text[k], fs=15.5)
    acc_y = ys[-1] - step
    _box(ax, 5.6, acc_y, 10.4, 0.56, "ACCEPT → action_accepted, SegmentRequest, LED + haptic", fc=STATE_FILL,
         ec="#0ca30c", fs=16, weight="bold")
    for a, b in zip([top] + ys, ys + [acc_y]):
        _arrow(ax, (5.6, a - 0.29), (5.6, b + 0.29))
    _box(ax, 13.9, np.mean(ys), 2.8, len(ys) * step, "REJECT\n\nLED reject flash\n\nREJECTED line\nnames the first\n"
                                                      f"of the top {named}\nthat fails; the\nlast {len(ys) - named} "
                                                      "print a\nbare REJECTED",
         fc=REJECT_FILL, ec=REJECT_EDGE, fs=16)
    for y in ys:
        _arrow(ax, (10.82, y), (12.48, y), color=S.ORANGE, lw=1.8)
    ax.text(11.65, ys[0] + 0.12, "fail", ha="center", va="bottom", fontsize=14, color=S.ORANGE)
    ax.plot([0.35, 10.85], [(ys[named - 1] + ys[named]) / 2] * 2, color=S.AXIS, lw=1.5, ls=(0, (4, 4)), zorder=0)
    files = S.save(fig, out, "09d_diagram_gate_pipeline")
    return _result("09d_diagram_gate_pipeline", "Validity-gate pipeline", fw, files,
                   f"{len(keys)} independent gates; a swing is accepted only if all pass.")


def all_diagrams(fw, out, gates_order, named):
    return [diagram_architecture(fw, out), diagram_phases(fw, out), diagram_loop(fw, out),
            diagram_gates(fw, out, gates_order, named)]
