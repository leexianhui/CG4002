"""End-to-end: synthetic fixture logs -> every figure is made without errors.

Renders into a temp folder (or $FIGPIPE_SMOKE_OUT to eyeball the layouts).
The fixture is fabricated; this checks code paths, not results.
"""

import os
import tempfile
import unittest

from figpipe import diagrams, figures, gates, logs
from figpipe.firmware import Firmware
from tests import synthetic_fixture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSmoke(unittest.TestCase):
    def test_all_figures_made(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "logs")
            out = os.environ.get("FIGPIPE_SMOKE_OUT") or os.path.join(tmp, "out")
            synthetic_fixture.write(log_dir)
            fw = Firmware(os.path.join(ROOT, "combined"))
            ds = logs.load(log_dir)
            self.assertEqual(ds.parse_errors, [])
            ev = gates.evaluate_all(ds.events, fw)
            self.assertEqual(len(ev), 8 * len(logs.ACTION_LABELS))
            self.assertTrue(ev["seg"].notna().all())
            ctx = figures.Context(fw=fw, ds=ds, ev=ev, out=out)
            res = (figures.fig_segmentation(ctx) + figures.fig_gates(ctx) + [figures.fig_outcomes(ctx)]
                   + [figures.fig_stillness(ctx), figures.fig_weapon_separation(ctx), figures.fig_lock_attempt(ctx),
                      figures.fig_el_az(ctx), figures.fig_peak_gyro(ctx), figures.fig_pose_singularity(ctx)]
                   + figures.fig_network(ctx) + diagrams.all_diagrams(fw, out, gates.GATES, gates.NAMED_BY_REJECT))
            skipped = [(r.stem, r.why_skipped) for r in res if r.status != "made"]
            self.assertEqual(skipped, [])
            for r in res:
                for f in r.files:
                    self.assertTrue(os.path.getsize(f) > 0, f)

    def test_labels_and_exclude(self):
        with tempfile.TemporaryDirectory() as tmp:
            synthetic_fixture.write(tmp)
            names = sorted(n for n in os.listdir(tmp) if n.endswith("_vertical_synthetic.csv"))
            with open(os.path.join(tmp, "exclude.txt"), "w") as f:
                f.write("# test\n" + names[0] + "\n")
            ds = logs.load(tmp)
            self.assertIn(names[0], ds.excluded)
            self.assertFalse((ds.lines.session == names[0]).any())
            self.assertIn("hold_spear", set(ds.lines.label.dropna()))
            self.assertIn("transition", set(ds.lines.label.dropna()))

    def test_empty_logs_skip_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = Firmware(os.path.join(ROOT, "combined"))
            ds = logs.load(tmp)
            ctx = figures.Context(fw=fw, ds=ds, ev=gates.evaluate_all(ds.events, fw), out=tmp)
            res = figures.fig_segmentation(ctx) + figures.fig_gates(ctx) + [figures.fig_outcomes(ctx)] + \
                figures.fig_network(ctx)
            self.assertTrue(all(r.status == "skipped" and r.why_skipped for r in res))


if __name__ == "__main__":
    unittest.main()
