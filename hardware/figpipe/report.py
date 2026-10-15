"""Writes figures/README.md and MISSING_DATA.md from the FigResults of a run."""

import os
from datetime import datetime

from .logs import ACTION_LABELS, HOLD_LABELS

LISTEN = ("Run both listeners for every capture, one session per label:\n"
          "`python listener.py --label <label>` and `python result_listener.py --label <label>`.\n"
          "If one session mixes labels, add time ranges to `logs/labels.csv` instead "
          "(see `logs/labels.example.csv`).")

HOLD_NOTE = ("The hub stops its pose stream the moment a weapon locks, which is "
             "`WEAPON_LOCK_HOLD_MS` into a steady hold. For long holds, temporarily raise "
             "`WEAPON_LOCK_HOLD_MS` in right_wrist_game.ino (e.g. to 600000) for this capture only, "
             "then put it back.")

# What each figure needs, keyed by stem. flags = firmware settings to flash.
NEEDS = {
    "01a_segmentation_swing": dict(
        what="Hub `[seg]` sample dumps of labelled swings (any of vertical/horizontal/diagonal/uppercut).",
        reps="≥ 5 reps of one swing type (the median-peak accepted rep is drawn).",
        flags="right_wrist_game.ino: `#define DEBUG_SEGMENT_DUMP true`. Leave 2–3 s between reps: each dump is "
              "one UDP line per sample (up to 500), and a rep whose dump lost lines is skipped."),
    "01b_segmentation_spin": dict(
        what="Hub `[seg]` dumps of reps labelled `spin`.", reps="≥ 5 reps.",
        flags="Same as 01a (`DEBUG_SEGMENT_DUMP true`)."),
    "01c_segmentation_thrust": dict(
        what="Hub `[seg]` dumps of reps labelled `thrust`.", reps="≥ 5 reps.",
        flags="Same as 01a (`DEBUG_SEGMENT_DUMP true`)."),
    "02a_gate_torso_peak": dict(
        what="`Motion ENDED` lines for every action label: " + ", ".join(f"`{x}`" for x in ACTION_LABELS) + ".",
        reps="≥ 15 reps per label (more for the fakes, which vary more).",
        flags="Default build. `DEBUG_SEGMENT_DUMP false` (keeps the debug stream light), `LATENCY_TEST 0`. "
              "All three boards on, weapon locked."),
    "02b_gate_total_rotation": dict(what="Same capture as 02a.", reps="Same as 02a.", flags="Same as 02a."),
    "02c_gate_raw_duration": dict(what="Same capture as 02a.", reps="Same as 02a.", flags="Same as 02a."),
    "02d_gate_swing_shape": dict(what="Same capture as 02a (uses swing-bucket reps only).", reps="Same as 02a.",
                                 flags="Same as 02a."),
    "02e_gate_spin_reversals": dict(
        what="Same capture as 02a; needs spin-likely reps (real `spin`, plus any fakes that rotate > the spin "
             "threshold, e.g. `fake_body_turn`).", reps="≥ 15 `spin` reps.", flags="Same as 02a."),
    "03_outcomes_by_label": dict(what="Same capture as 02a.", reps="Same as 02a.", flags="Same as 02a."),
    "04_stillness_ema": dict(
        what="Pose-stream lines from all three boards during stance holds (`hold_sword_shield`, `hold_spear`, "
             "`hold_mace`) and while moving between stances (`transition`).",
        reps="Per weapon: 5 holds of ~10 s, with transitions between them. Label holds and transitions with "
             "time ranges in logs/labels.csv.",
        flags="Defaults: hub `DEBUG_POSE_STREAM true`, torso `DEBUG_POSE_STREAM true`, left wrist "
              "`DEBUG_RAW_STREAM true`. " + HOLD_NOTE),
    "05b_weapon_lock_attempt": dict(
        what="One normal boot → weapon lock with the pose stream on (el/az format: `raw rEl=... nearest=... "
             "dist=...`).", reps="3 lock attempts (the last one that locks is drawn).",
        flags="Default build (`DEBUG_POSE_STREAM true`). Run result_listener.py too so `weapon_locked` is "
              "captured."),
    "05c_weapon_el_az_scatter": dict(
        what="Hub pose-stream lines labelled `hold_sword_shield` / `hold_spear` / `hold_mace`, with all three "
             "boards on (so the left wrist is fresh).",
        reps="Per weapon: 5 holds of ~10 s. Can be the same capture as 04. Run `python weapon_refs.py` on "
             "these sessions to (re)compute WEAPON_REFS whenever a board's mounting changes.",
        flags="Same as 04."),
    "06_peak_gyro_range": dict(
        what="Any `Motion ENDED` lines from the current build (the 02a capture is enough). Optional overlay: a "
             "capture from a ±250 dps build, with `gyro250` in its session label.",
        reps="≥ 50 motion events.",
        flags="Default build. For the optional overlay only: a temporary build with "
              "`writeRegister(REG_GYRO_CONFIG, 0x00)` and `GYRO_SCALE = 131.0` in right_wrist_game.ino, run "
              "with `--label gyro250`."),
    "08a_network_rtt": dict(
        what="`[rtt]` lines, one capture with WiFi sleep on and one with it off.",
        reps="≥ 5 minutes each after the weapon locks (≈ 3000 pings each).",
        flags="`LATENCY_TEST 1` on hub AND torso. All three boards with `WIFI_SLEEP_DISABLED 1` (sleep off), "
              "then all three with `WIFI_SLEEP_DISABLED 0` (sleep on). The `[cfg]` boot line tags each session "
              "automatically; label them e.g. `--label sleep_off`."),
    "08b_network_beacon_iat": dict(
        what="`[beacon_iat]` lines (hub, after the weapon locks), sleep on and off.",
        reps="Same captures as 08a.", flags="Same as 08a."),
    "08c_network_packet_loss": dict(
        what="`[stats]` lines from all three boards, sleep on and off.",
        reps="Same captures as 08a (every build logs `[stats]` once a second).", flags="Same as 08a."),
}


def _md_list(items):
    return "\n".join(f"- {x}" for x in items) if items else "- none"


def write_readme(results, fw, ds, out_dir, excluded):
    made = [r for r in results if r.status == "made"]
    lines = [
        "# Figures",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `python make_figures.py`. Do not edit by hand — rerun "
        "instead. Every PNG is 1920×1080 and has a matching SVG.",
        "",
        f"**Made:** {len(made)} of {len(results)} figures. Figures that need data you don't have yet are "
        "listed in [MISSING_DATA.md](../MISSING_DATA.md).",
        "",
        f"**Logs used:** {len([s for s in ds.sessions])} session file(s). "
        f"**Excluded** (logs/exclude.txt): {', '.join(excluded) if excluded else 'none'}.",
        "",
        "Colours: blue = real action, orange = fake / guard raise (each label is named on the axis). "
        "Weapons: blue = sword & shield, orange = spear, aqua = mace. WiFi: blue = sleep off, orange = sleep on. "
        "Dashed lines are thresholds, with the constant's name and current value written on them.",
        "",
    ]
    if ds.parse_errors:
        lines += ["**Parse errors** (lines skipped):", "", _md_list(ds.parse_errors[:20]), ""]
    for r in results:
        lines.append(f"## {r.stem} — {r.title}")
        lines.append("")
        if r.status != "made":
            lines += [f"*Not made:* {r.why_skipped}. See MISSING_DATA.md.", ""]
            continue
        pngs = [os.path.basename(f) for f in r.files if f.endswith(".png")]
        lines.append("Files: " + ", ".join(f"`{p}`" for p in pngs) + " (+ .svg)")
        lines.append("")
        lines.append("**Sources:** " + ", ".join(f"`{s}`" for s in r.sources))
        lines.append("")
        if r.counts:
            lines.append("**Samples:** " + ", ".join(f"{k}: {v}" for k, v in r.counts.items()))
            lines.append("")
        if r.takeaway:
            lines.append(f"**Takeaway:** {r.takeaway}")
            lines.append("")
        if r.caveats:
            lines.append("**Caveats:**")
            lines.append("")
            lines.append(_md_list(r.caveats))
            lines.append("")
    lines += ["## Firmware constants used for this run", "",
              "Read from the .ino files at build time (not hard-coded):", "",
              "| Constant | right wrist | left wrist | torso |", "|---|---|---|---|"]
    names = ["START_THRESHOLD", "JERK_START_THRESHOLD", "TORSO_GATE_MAX_NORMAL", "TORSO_GATE_MAX_SPIN",
             "MIN_TORSO_ENGAGEMENT", "RAW_MAX_DURATION_MS", "SWING_MAX_REVERSALS", "SPIN_MAX_REVERSALS",
             "SWING_MIN_SUSTAIN_FRACTION", "THRUST_MAX_ROTATION_DEG", "SPIN_RAW_ROTATION_DEG", "STILLNESS_GYRO_THRESHOLD_DPS",
             "WEAPON_MATCH_MAX_DEG", "WEAPON_MATCH_MARGIN_DEG", "WEAPON_LOCK_HOLD_MS", "SETTLE_FRACTION", "PRE_PEAK_MS", "POST_PEAK_MS"]
    for n in names:
        cells = [str(fw.values[node].get(n, "")) for node in ("right_wrist", "left_wrist", "torso")]
        lines.append(f"| {n} | " + " | ".join(cells) + " |")
    for func, vals in sorted(fw.local["right_wrist"].items()):
        if func in ("isValidAttack", "matchesKnownPattern"):
            for n, v in vals.items():
                lines.append(f"| {func}: {n} | {v} | | |")
    lines += ["", "WEAPON_REFS: " + "; ".join(
        f"{x['weapon']} right=({x['rEl']:g}, {x['rAz']:g}) left=({x['lEl']:g}, {x['lAz']:g})"
        for x in fw.weapon_refs), ""]
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_missing(results, path):
    missing = [r for r in results if r.status != "made"]
    lines = ["# Missing data checklist", "",
             f"Generated {datetime.now():%Y-%m-%d %H:%M} by `python make_figures.py`; it lists only the "
             "figures that couldn't be made from the current logs. Tick items off by capturing, then rerun.",
             "", LISTEN, "",
             "Labels: " + ", ".join(f"`{x}`" for x in ACTION_LABELS + HOLD_LABELS + ["transition"]) + ".",
             "",
             "Firmware flags: `DEBUG_SEGMENT_DUMP`, `DEBUG_POSE_STREAM` and `DEBUG_RAW_STREAM` are plain "
             "`#define`s — edit the line. `LATENCY_TEST` and `WIFI_SLEEP_DISABLED` are `#ifndef` defaults — edit "
             "the default or pass `-DLATENCY_TEST=1` as a build flag.", ""]
    if not missing:
        lines.append("Nothing missing — every figure was made.")
    for r in missing:
        need = NEEDS.get(r.stem, {})
        lines += [f"## [ ] {r.stem} — {r.title}", "", f"Why it's missing: {r.why_skipped}.", ""]
        if need:
            lines += [f"- **Record:** {need['what']}", f"- **How many:** {need['reps']}",
                      f"- **Flags:** {need['flags']}", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
