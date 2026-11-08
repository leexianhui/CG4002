"""Regression features copied from P2 handling and committed-attack captures."""

import csv
from pathlib import Path
import unittest

from figpipe.handling import rotation_reject


FIXTURE = Path(__file__).with_name("fixtures") / "p2_handling.csv"


class TestHandling(unittest.TestCase):
    def test_recorded_strokes(self):
        with FIXTURE.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertGreater(len(rows), 30)
        for row in rows:
            with self.subTest(session=row["session"], t0=row["t0"]):
                reason = rotation_reject(
                    row["weapon"], row["action"],
                    [float(row[k]) for k in ("ax", "ay", "az")],
                    [float(row[k]) for k in ("px", "py", "pz")],
                    float(row["peak"]), float(row["t1"]) - float(row["t0"]))
                self.assertEqual(reason, row["reject"] or None)

    def test_sword_and_spin_are_exempt(self):
        self.assertIsNone(rotation_reject("sword_shield", "uppercut",
                                         (0.13, 0.47, 0.873), (0.6, 0.7, -0.3), 600, 350))
        self.assertIsNone(rotation_reject("spear", "spin",
                                         (-0.36, -0.29, -0.887), (0.2, 0.97, 0.1), 550, 350))

    def test_hubs_share_the_policy(self):
        root = Path(__file__).resolve().parents[1] / "2 players"
        policies = []
        for player in (1, 2):
            source = (root / f"p{player}_right_wrist" / f"p{player}_right_wrist.ino").read_text()
            policies.append(source.split("const char *handlingReject(", 1)[1].split(
                "void onMotionStart(", 1)[0])
            self.assertIn('else if (handling)\n            reason = handling;', source)
        self.assertEqual(*policies)


if __name__ == "__main__":
    unittest.main()
