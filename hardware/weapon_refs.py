"""Computes WEAPON_REFS from recorded stance holds and prints a block to paste
into combined/right_wrist_game.ino.

    python weapon_refs.py                  # uses logs/
    python weapon_refs.py --logs other/

Input: hub pose-stream lines ("raw rEl=... | left el=...") from sessions
labelled hold_sword_shield / hold_spear / hold_mace (listener --label, or
time ranges in logs/labels.csv). Only samples where both wrists are fresh and
stable are used, i.e. the ones the firmware itself would classify.

Each reference is the mean gravity DIRECTION (unit vectors averaged, then
converted back to el/az), not the mean of el and az, so azimuth wrap-around
at +/-180 can't corrupt it. Then it checks what the firmware needs:
  - within-hold spread: how far samples sit from their own reference
  - matched: share of each weapon's own hold samples that the firmware's rule
    (classifyWeapon: nearest weapon <= WEAPON_MATCH_MAX_DEG and
    >= WEAPON_MATCH_MARGIN_DEG closer than the next) would accept with the new
    references
  - separation: straight toward another weapon a pose may drift
    (separation - WEAPON_MATCH_MARGIN_DEG) / 2; that has to cover both
    weapons' p95 spread
"""

import argparse
import sys
from datetime import date

import numpy as np

from figpipe import logs
from figpipe.firmware import Firmware, classify_weapon, grav_from_el_az, weapon_distance

WEAPONS = ["sword_shield", "spear", "mace"]  # firmware enum order
ENUM = {"sword_shield": "WEAPON_SWORD_SHIELD", "spear": "WEAPON_SPEAR", "mace": "WEAPON_MACE"}
# A hold session also contains the rest pose between holds; samples further
# than this from the first estimate are dropped as rest pose.
REST_DROP_DEG = 40.0


def mean_direction(el, az):
    v = np.array([grav_from_el_az(e, a) for e, a in zip(el, az)]).mean(axis=0)
    v /= np.linalg.norm(v)
    return float(np.degrees(np.arcsin(np.clip(v[2], -1, 1)))), float(np.degrees(np.arctan2(v[1], v[0])))


def estimate(weapon, d):
    r_el, r_az = mean_direction(d.rEl, d.rAz)
    l_el, l_az = mean_direction(d.l_el, d.l_az)
    return {"weapon": weapon, "rEl": r_el, "rAz": r_az, "lEl": l_el, "lAz": l_az}


def distances(d, ref):
    return np.array([weapon_distance(a, b, c, e, ref)[0] for a, b, c, e in zip(d.rEl, d.rAz, d.l_el, d.l_az)])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default="logs")
    ap.add_argument("--firmware", default="combined")
    ap.add_argument("--min-samples", type=int, default=20,
                    help="stable pose samples needed per weapon (pose stream is ~3/s)")
    args = ap.parse_args()

    fw = Firmware(args.firmware)
    match, margin = fw.rw("WEAPON_MATCH_MAX_DEG"), fw.rw("WEAPON_MATCH_MARGIN_DEG")
    ds = logs.load(args.logs)
    L = ds.lines
    if L.empty or "rStable" not in L:  # rEl alone also appears on weapon_candidate result lines
        sys.exit("No hub pose-stream lines found. Record hold_<weapon> sessions first.")
    hp = L[(L.kind == "hub_pose") & (L.rStable == 1) & (L.l_fresh == 1) & (L.l_stable == 1)]

    refs, missing = {}, []
    for w in WEAPONS:
        d = hp[hp.label == f"hold_{w}"]
        if len(d) < args.min_samples:
            missing.append(f"{w} ({len(d)} stable samples, need {args.min_samples})")
            continue
        # A session labelled hold_<weapon> also contains the rest pose between
        # holds (still long enough to read as stable). Estimate once, drop
        # samples > REST_DROP_DEG from it, estimate again.
        n_all = len(d)
        ref = estimate(w, d)
        keep = distances(d, ref) <= REST_DROP_DEG
        if not keep.all() and keep.sum() >= args.min_samples:
            d = d[keep]
            ref = estimate(w, d)
        dist = distances(d, ref)
        ref.update(n=len(d), dropped=n_all - len(d), sessions=sorted(d.session.unique()),
                   p50=np.median(dist), p95=np.percentile(dist, 95), samples=d)
        refs[w] = ref

    ref_list = [{k: r[k] for k in ("weapon", "rEl", "rAz", "lEl", "lAz")} for r in refs.values()]
    print(f"Match rule: nearest weapon <= {match:g} deg and >= {margin:g} deg closer than the next.")
    for w, r in refs.items():
        d = r["samples"]
        hits = [classify_weapon(a, b, c, e, ref_list, match, margin)[0] == w
                for a, b, c, e in zip(d.rEl, d.rAz, d.l_el, d.l_az)]
        r["matched"] = 100.0 * np.mean(hits)
        print(f"{w:13s} n={r['n']:4d} (dropped {r['dropped']} > {REST_DROP_DEG:g} deg away)  right=({r['rEl']:6.1f}, {r['rAz']:6.1f})  "
              f"left=({r['lEl']:6.1f}, {r['lAz']:6.1f})  spread p50={r['p50']:.1f} p95={r['p95']:.1f} deg  "
              f"matched {r['matched']:.0f}%  [{', '.join(r['sessions'])}]")
        if r["matched"] < 90:
            print(f"  ! only {r['matched']:.0f}% of this weapon's own hold samples would match - holds too loose, "
                  "too close to another weapon, or the stance changed between reps; the lock will keep dropping.")
    if missing:
        print("\nMissing:", "; ".join(missing))

    ok = True
    if len(refs) >= 2:
        print(f"\nSeparation (drift allowed straight toward the other = (separation - {margin:g}) / 2; "
              "needs to cover both weapons' p95 spread):")
        names = list(refs)
        for i in range(len(names)):
            for k in range(i + 1, len(names)):
                a, b = refs[names[i]], refs[names[k]]
                d, dr, dl = weapon_distance(a["rEl"], a["rAz"], a["lEl"], a["lAz"], b)
                allow = max(d - margin, 0) / 2
                need = max(a["p95"], b["p95"])
                flag = "ok" if allow >= need else "TOO CLOSE"
                ok &= allow >= need
                print(f"  {names[i]:13s} - {names[k]:13s} {d:6.1f} deg (right {dr:.1f}, left {dl:.1f})  "
                      f"drift allowed {allow:.1f} vs p95 {need:.1f}  {flag}")

    if len(refs) == len(WEAPONS):
        print(f"\n// Measured {date.today():%Y-%m-%d} from the el/az pose stream "
              f"(weapon_refs.py, {sum(r['n'] for r in refs.values())} stable samples).")
        print("const WeaponRef WEAPON_REFS[] = {")
        for w in WEAPONS:
            r = refs[w]
            print(f"    {{{ENUM[w]}, {r['rEl']:.1f}, {r['rAz']:.1f}, {r['lEl']:.1f}, {r['lAz']:.1f}}},")
        print("};")
        if not ok:
            print("\n! At least one pair is too close: normal holds of those weapons would read as ambiguous "
                  "and not lock. Change one of those stances (or the board mounting) and re-record before "
                  "using this table.")


if __name__ == "__main__":
    main()
