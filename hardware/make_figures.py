"""Regenerates every video figure from the logs and the firmware.

    python make_figures.py                 # logs/ + combined/*.ino -> figures/
    python make_figures.py --logs other/   # a different log folder

Outputs figures/NN_*.png (1920x1080) + .svg, figures/README.md and
MISSING_DATA.md. Figures whose data isn't in the logs yet are skipped and
listed in MISSING_DATA.md with what to record; nothing is invented.
Needs pandas + matplotlib (pip install -r requirements.txt).
"""

import argparse
import glob
import os
import sys

from figpipe import diagrams, figures, gates, logs, report
from figpipe.firmware import Firmware


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default="logs")
    ap.add_argument("--firmware", default="combined")
    ap.add_argument("--out", default="figures")
    ap.add_argument("--missing", default="MISSING_DATA.md")
    args = ap.parse_args()

    fw = Firmware(args.firmware)
    ds = logs.load(args.logs)
    ev = gates.evaluate_all(ds.events, fw)

    os.makedirs(args.out, exist_ok=True)
    for old in glob.glob(os.path.join(args.out, "[0-9][0-9]*_*.*")):
        os.remove(old)  # a figure that can no longer be made must not linger from an earlier run

    ctx = figures.Context(fw=fw, ds=ds, ev=ev, out=args.out)
    results = []
    results += figures.fig_segmentation(ctx)
    results += figures.fig_gates(ctx)
    results.append(figures.fig_outcomes(ctx))
    results.append(figures.fig_stillness(ctx))
    results.append(figures.fig_weapon_separation(ctx))
    results.append(figures.fig_lock_attempt(ctx))
    results.append(figures.fig_el_az(ctx))
    results.append(figures.fig_peak_gyro(ctx))
    results.append(figures.fig_pose_singularity(ctx))
    results += figures.fig_network(ctx)
    results += diagrams.all_diagrams(fw, args.out, gates.GATES, gates.NAMED_BY_REJECT)

    report.write_readme(results, fw, ds, args.out, ds.excluded)
    report.write_missing(results, args.missing)

    print(f"sessions: {len(ds.sessions)} used, {len(ds.excluded)} excluded; motion events: {len(ev)}")
    for r in results:
        print(f"  {'made   ' if r.status == 'made' else 'skipped'}  {r.stem}"
              + ("" if r.status == "made" else f"  ({r.why_skipped})"))
    if ds.parse_errors:
        print(f"{len(ds.parse_errors)} parse error(s) - see {args.out}/README.md", file=sys.stderr)
    print(f"wrote {args.out}/README.md and {args.missing}")


if __name__ == "__main__":
    main()
