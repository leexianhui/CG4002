"""Constant parsing, the firmware distance formula, and gate recomputation."""

import os
import tempfile
import unittest

from figpipe import firmware as F
from figpipe import gates, parsers
from tests.test_parsers import SAMPLE_ENDED

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SNIPPET = """
#define DEBUG_SEGMENT_DUMP false // full sample dump
#ifndef LATENCY_TEST
#define LATENCY_TEST 0
#endif
const float START_THRESHOLD = 60.0;   // comment with { brace
const unsigned long RAW_MAX_DURATION_MS = 3500UL;
static_assert(sizeof(HubStatus) == 8, "x");
/* const float COMMENTED_OUT = 1.0; */
bool isValidAttack(MotionStats s, MotionShape shape)
{
    const float MIN_PEAK_GYRO_DPS = 180.0;
    if (s.duration < 1) { return false; }
    return true;
}
const int AFTER_FUNCTION = 3;
"""


class TestParseSource(unittest.TestCase):
    def test_snippet(self):
        consts, defines, local, sizes = F.parse_source(SNIPPET)
        self.assertEqual(consts["START_THRESHOLD"], 60.0)
        self.assertEqual(consts["RAW_MAX_DURATION_MS"], 3500)
        self.assertEqual(consts["AFTER_FUNCTION"], 3)
        self.assertNotIn("MIN_PEAK_GYRO_DPS", consts)
        self.assertNotIn("COMMENTED_OUT", consts)
        self.assertEqual(local["isValidAttack"]["MIN_PEAK_GYRO_DPS"], 180.0)
        self.assertEqual(defines["DEBUG_SEGMENT_DUMP"], False)
        self.assertEqual(defines["LATENCY_TEST"], 0)
        self.assertEqual(sizes["HubStatus"], 8)

    def test_real_firmware_loads(self):
        fw = F.Firmware(os.path.join(ROOT, "combined"))
        for node, names in F.REQUIRED.items():
            for n in names:
                self.assertIsInstance(fw.c(node, n), (int, float, bool), n)
        self.assertGreaterEqual(len(fw.weapon_refs), 3)

    def test_missing_constant_is_loud(self):
        with tempfile.TemporaryDirectory() as d:
            for fname in F.NODE_FILES.values():
                with open(os.path.join(d, fname), "w") as f:
                    f.write("const float NOTHING = 1.0;\n")
            with self.assertRaises(F.FirmwareError):
                F.Firmware(d)


class TestDistance(unittest.TestCase):
    REFS = [{"weapon": "a", "rEl": -15.0, "rAz": 5.3, "lEl": 50.1, "lAz": -14.7},
            {"weapon": "b", "rEl": -1.3, "rAz": -69.8, "lEl": -5.0, "lAz": -1.3}]

    def test_self_zero_and_symmetric(self):
        a, b = self.REFS
        self.assertAlmostEqual(F.weapon_distance(a["rEl"], a["rAz"], a["lEl"], a["lAz"], a)[0], 0, places=3)
        dab = F.weapon_distance(a["rEl"], a["rAz"], a["lEl"], a["lAz"], b)[0]
        dba = F.weapon_distance(b["rEl"], b["rAz"], b["lEl"], b["lAz"], a)[0]
        self.assertAlmostEqual(dab, dba, places=6)

    def test_pure_elevation_change(self):
        ref = {"rEl": 0, "rAz": 0, "lEl": 0, "lAz": 0}
        d, dr, dl = F.weapon_distance(30, 0, 0, 0, ref)
        self.assertAlmostEqual(dr, 30, places=4)
        self.assertAlmostEqual(dl, 0, places=4)
        self.assertAlmostEqual(d, 30, places=4)

    def test_classify_margin_rule(self):
        refs = [{"weapon": "a", "rEl": 0, "rAz": 0, "lEl": 0, "lAz": 0},
                {"weapon": "b", "rEl": 50, "rAz": 0, "lEl": 0, "lAz": 0}]
        # 30 deg off "a", away from "b": beyond an old 20 deg radius, but unambiguous
        w, nearest, dist, margin = F.classify_weapon(-30, 0, 0, 0, refs, 35, 15)
        self.assertEqual((w, nearest), ("a", "a"))
        self.assertAlmostEqual(dist, 30, places=3)
        self.assertAlmostEqual(margin, 50, places=3)
        # halfway between: nearest is "a" by a hair, not a match
        self.assertIsNone(F.classify_weapon(24, 0, 0, 0, refs, 35, 15)[0])
        # too far from everything
        self.assertIsNone(F.classify_weapon(-40, 0, 0, 0, refs, 35, 15)[0])

    def test_classify_several_refs_per_weapon(self):  # runner-up must be another WEAPON, not another ref
        refs = [{"weapon": "a", "rEl": 0, "rAz": 0, "lEl": 0, "lAz": 0},
                {"weapon": "a", "rEl": 10, "rAz": 0, "lEl": 0, "lAz": 0},
                {"weapon": "b", "rEl": 60, "rAz": 0, "lEl": 0, "lAz": 0}]
        self.assertEqual(F.classify_weapon(5, 0, 0, 0, refs, 35, 15)[0], "a")

    def test_margin_constant_parsed(self):
        fw = F.Firmware(os.path.join(ROOT, "combined"))
        self.assertGreater(fw.rw("WEAPON_MATCH_MARGIN_DEG"), 0)

    def test_azimuth_shrinks_near_pole(self):  # real angle, not raw az difference
        ref = {"rEl": 80, "rAz": 0, "lEl": 0, "lAz": 0}
        self.assertLess(F.weapon_distance(80, 90, 0, 0, ref)[1], 20)


class TestGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fw = F.Firmware(os.path.join(ROOT, "combined"))

    def test_sample_line_matches_logged_flags(self):
        e = parsers.parse_motion_ended(SAMPLE_ENDED)
        g = gates.evaluate(e, self.fw)
        self.assertEqual(g["drift"], [])
        self.assertFalse(g["torso_engagement"])  # torsoPeak 5.0 < MIN_TORSO_ENGAGEMENT
        self.assertFalse(g["accept"])
        self.assertEqual(g["accept"], g["logged_accept"])

    def test_all_gates_reported(self):
        e = parsers.parse_motion_ended(SAMPLE_ENDED)
        e.update(torsoPeak=5000.0, rawDur=10 ** 6)  # fail engagement? no - fails ceiling and raw duration
        g = gates.evaluate(e, self.fw)
        self.assertFalse(g["torso_ceiling"])
        self.assertFalse(g["raw_duration"])
        self.assertTrue(g["torso_engagement"])

    def test_magnitude_needs_gyro_and_accel(self):
        e = parsers.parse_motion_ended(SAMPLE_ENDED)
        self.assertTrue(gates.evaluate(e, self.fw)["magnitude"])
        low_accel = dict(e, peakAccel=self.fw.fn("isValidAttack", "MIN_PEAK_JERK_G") - 0.01)
        self.assertFalse(gates.evaluate(low_accel, self.fw)["magnitude"])  # high gyro alone isn't enough
        low_gyro = dict(e, peak=self.fw.fn("isValidAttack", "MIN_PEAK_GYRO_DPS") - 1)
        self.assertFalse(gates.evaluate(low_gyro, self.fw)["magnitude"])

    def test_spin_exemptions(self):
        e = parsers.parse_motion_ended(SAMPLE_ENDED)
        e.update(rawTotalRot=self.fw.rw("SPIN_RAW_ROTATION_DEG") + 1, torsoPeak=1.0, reversals=0)
        g = gates.evaluate(e, self.fw)
        self.assertEqual(g["bucket"], "spin")
        self.assertTrue(g["torso_engagement"])  # waived for spin
        self.assertTrue(g["spin_reversal"])
        e["reversals"] = self.fw.rw("SPIN_MAX_REVERSALS")
        self.assertTrue(gates.evaluate(e, self.fw)["spin_reversal"])
        e["reversals"] = self.fw.rw("SPIN_MAX_REVERSALS") + 1
        self.assertFalse(gates.evaluate(e, self.fw)["spin_reversal"])


if __name__ == "__main__":
    unittest.main()
