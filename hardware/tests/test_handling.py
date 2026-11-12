"""Regression features copied from P2 handling and committed-attack captures."""

import csv
import re
from pathlib import Path
import unittest

import figpipe.handling as handling
from figpipe.handling import (push_commit_reject, rotation_commit_reject, rotation_reject,
                              torso_quiet_cut)


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


SKETCHES = Path(__file__).resolve().parents[1] / "2 players"
COMMIT_CONSTANTS = ("STRIKE_MIN_ROT_DEG", "SLOW_MAX_PEAK_DPS", "SLOW_MIN_DURATION_MS",
                    "SLOWER_MAX_PEAK_DPS", "SLOWER_MIN_DURATION_MS", "SPIN_MIN_ROT_DEG",
                    "RAISE_AXIS_MIN_Z", "RAISE_MAX_ROT_DEG", "RAISE_MAX_TORSO_RATIO",
                    "RAISE_MIN_TORSO_RATIO", "THRUST_MIN_REACH_M", "THRUST_MIN_PEAK_G",
                    "THRUST_MAX_WRIST_DPS", "CUT_MIN_TORSO_RATIO", "THRUST_FLICK_MAX_REACH_M",
                    "CUT_TORSO_EXEMPT_ROT_DEG", "CUT_TORSO_EXEMPT_PEAK_DPS")


class TestCommitmentGate(unittest.TestCase):
    """Strokes taken from the 2026-11-12 P1 captures and the November P2/P1 sessions."""

    def test_flick_uppercut(self):  # fake_flick_sword: 28 deg in 221 ms
        self.assertEqual(rotation_commit_reject(202, 28, 221, (-0.29, 0.38, 0.88), 0.04), "flick")

    def test_slow_backswing(self):  # horizontal_normal_mace back-swing accepted as uppercut
        self.assertEqual(rotation_commit_reject(177, 106, 962, (-0.76, 0.38, 0.52), 0.18), "slow")

    def test_guard_raise(self):  # fake_guard_raise_spear
        self.assertEqual(rotation_commit_reject(294, 87, 451, (0.42, 0.17, 0.89), 0.07), "guard_raise")
        # fake_guard_raise_sword: full rotation but no torso at all
        self.assertEqual(rotation_commit_reject(361, 130, 552, (-0.13, 0.14, 0.98), 0.03), "guard_raise")

    def test_real_strokes_pass(self):
        self.assertIsNone(rotation_commit_reject(404, 137, 521, (-0.19, 0.32, 0.93), 0.22))  # P1 uppercut
        self.assertIsNone(rotation_commit_reject(277, 122, 672, (0.0, 0.5, 0.79), 0.12))     # slow P2 uppercut
        self.assertIsNone(rotation_commit_reject(282, 99, 641, (0.95, -0.01, -0.3), 0.16))   # spear horizontal
        self.assertIsNone(rotation_commit_reject(244, 368, 2005, (0.1, 0.2, -0.78), 0.78))   # spin

    def test_thrust_gate(self):
        self.assertEqual(push_commit_reject(1.29, 0.29), "short")  # fake_flick_spear
        self.assertEqual(push_commit_reject(0.68, 0.52), "short")  # fake_guard_raise_sword
        self.assertIsNone(push_commit_reject(1.25, 0.79))          # P1 sword thrust

    def test_thrust_flick(self):  # 2026-11-12 11:14 fake_flick_spear vs a real mace thrust
        self.assertEqual(push_commit_reject(2.11, 0.49, 420), "flick")
        self.assertIsNone(push_commit_reject(2.33, 0.73, 250))
        # 2026-11-12 11:58 thrust_mace: a hard real thrust turns the wrist fast but travels far
        self.assertIsNone(push_commit_reject(2.21, 0.76, 380))

    def test_torso_quiet_cut(self):  # spear flick "horizontal" vs a real spear horizontal
        self.assertTrue(torso_quiet_cut("horizontal", 0.04))
        self.assertTrue(torso_quiet_cut("diagonal", 0.07))
        self.assertFalse(torso_quiet_cut("horizontal", 0.16))
        self.assertFalse(torso_quiet_cut("vertical", 0.06))  # real verticals go this low
        # 2026-11-12 12:43 sword vertical matched as diagonal: full power, little torso
        self.assertFalse(torso_quiet_cut("diagonal", 0.05, 156, 747))
        self.assertTrue(torso_quiet_cut("horizontal", 0.04, 90, 340))  # spear flick

    def test_constants_match_sketches(self):
        for player in (1, 2):
            source = (SKETCHES / f"p{player}_right_wrist" / f"p{player}_right_wrist.ino").read_text()
            for name in COMMIT_CONSTANTS:
                with self.subTest(player=player, name=name):
                    match = re.search(rf"const float {name} = ([0-9.]+)f?;", source)
                    self.assertIsNotNone(match)
                    self.assertAlmostEqual(float(match.group(1)), getattr(handling, name))
            self.assertIn("const uint32_t GUARD_EDGE_MS = 600;", source)
            self.assertIn("const uint32_t GUARD_STALE_MS = 1500;", source)


class TestGuardSettings(unittest.TestCase):
    def test_sketch_matches_replay(self):
        import guard_replay
        names = {"WEAPON_SWORD_SHIELD": "sword", "WEAPON_SPEAR": "spear", "WEAPON_MACE": "mace"}
        expected = {w: sorted(r) for w, r in guard_replay.NEW["refs"].items()}
        for player in (1, 2):
            source = (SKETCHES / f"p{player}_left_wrist" / f"p{player}_left_wrist.ino").read_text()
            found = {}
            for m in re.finditer(r"\{(WEAPON_\w+), (-?[0-9.]+)f, (-?[0-9.]+)f, ([0-9.]+)f, ([0-9.]+)f, "
                                 r"([0-9.]+)f, ([0-9.]+)f\}", source):
                found.setdefault(names[m.group(1)], []).append(tuple(float(x) for x in m.groups()[1:]))
            self.assertEqual({w: sorted(r) for w, r in found.items()}, expected)
            for name, key in (("GUARD_STILL_DPS", "still_dps"), ("GUARD_HOLD_MS", "hold_ms"),
                              ("GUARD_ARM_MS", "arm_ms")):
                match = re.search(rf"const (?:float|unsigned long) {name} = ([0-9.]+)f?;", source)
                self.assertAlmostEqual(float(match.group(1)), guard_replay.NEW[key], msg=name)


if __name__ == "__main__":
    unittest.main()
