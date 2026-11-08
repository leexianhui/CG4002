"""Reviewed November P2 features: raises must not poison strike history.

Fake captures are entirely negative. Diagonal targets are inferred downward
strokes, not externally timed ground truth. The fixture also keeps all pushes.
"""
from pathlib import Path
import unittest

import pandas as pd
from figpipe import raw, replay

ROOT = Path(__file__).resolve().parents[1]
SKETCH = ROOT / "2 players/p2_right_wrist/p2_right_wrist.ino"


def recordings():
    df = pd.read_csv(Path(__file__).with_name("fixtures") / "p2_preparation.csv")
    df = df.rename(columns=dict(ax="ux", ay="uy", az="uz", px="hx", py="hy",
                               pz="hz", torsoRatio="torso_ratio", beacons="torso_beacons"))
    df["dur"] = df.t1 - df.t0
    return df


class TestPreparation(unittest.TestCase):
    def test_p2_policy_covers_original_and_fallback_without_changing_p1(self):
        rows = recordings()
        # First is an original uppercut match; second exactly matches an old
        # (incorrectly labelled) diagonal fallback exemplar.
        for t0 in (260360, 135357):
            with self.subTest(t0=t0):
                stroke = rows[(rows.t0 == t0) & (rows.kind == "rot")]
                self.assertEqual(len(stroke), 1)
                self.assertTrue(replay.firmware_decide(stroke, SKETCH, "mace", 1).accept.all())
                self.assertFalse(replay.firmware_decide(stroke, SKETCH, "mace", 2).accept.any())

    def test_recorded_decisions(self):
        for name, group in recordings().groupby("session"):
            with self.subTest(session=name):
                result = replay.firmware_decide(group, SKETCH, group.weapon.iloc[0], 2)
                marked = result[result.expected.notna()]
                wrong = marked[marked.accept != marked.expected.astype(bool)]
                self.assertTrue(wrong.empty, wrong[["t0", "kind", "nearest", "reason",
                                                       "dist", "expected"]].to_string(index=False))

    def test_local_raw_recordings(self):
        captures = ROOT / "2 players/logs/p2"
        expected = recordings()
        names = set(expected.session[expected.session.str.startswith("20261108")])
        sessions = [s for s in raw.load_sessions(captures) if s.name in names]
        if len(sessions) != len(names):
            self.skipTest("November 8 raw recordings absent or archived")
        for session in sessions:
            with self.subTest(session=session.name):
                result = replay.firmware_decide(replay.replay_session(session),
                                                SKETCH, session.weapon, 2)
                accepted = result[result.accept]
                targets = expected[(expected.session == session.name) & (expected.expected == 1)]
                if targets.empty:
                    self.assertTrue(accepted.empty, accepted[["t0", "kind", "nearest"]].to_string())
                else:
                    self.assertEqual(len(accepted), len(targets))
                    for _, target in targets.iterrows():
                        self.assertEqual(int(((accepted.t0 < target.t1) &
                                              (target.t0 < accepted.t1)).sum()), 1)


if __name__ == "__main__":
    unittest.main()
