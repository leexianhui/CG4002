"""Optional end-to-end replay of local P2 recordings (captures are gitignored)."""

from pathlib import Path
import unittest

from figpipe import raw, replay


ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "2 players" / "logs" / "p2"
SKETCH = ROOT / "2 players" / "p2_right_wrist" / "p2_right_wrist.ino"


@unittest.skipUnless(CAPTURES.is_dir(), "local P2 captures absent")
class TestHandlingRecordings(unittest.TestCase):
    def test_raw_captures(self):
        names = {"20261108_214230_guard_raise_mace",
                 "20261108_214810_guard_raise_spear",
                 "20261108_215011_uppercut_pullback_spear"}
        sessions = [s for s in raw.load_sessions(CAPTURES) if s.name in names]
        if len(sessions) != len(names):
            self.skipTest("November 8 regression captures absent or archived")
        for session in sessions:
            with self.subTest(session=session.name):
                strokes = replay.replay_session(session)
                decisions = replay.firmware_decide(strokes, SKETCH, session.weapon, 2)
                accepted = decisions[decisions.accept]
                print(f"\n{session.name}: {len(accepted)} accepted; "
                      f"{decisions.reason.value_counts().to_dict()}")
                if "guard_raise" in session.name:
                    self.assertTrue(accepted.empty, accepted[[
                        "t0", "t1", "dur", "peak", "ux", "uy", "uz", "hx", "hy", "hz",
                        "kind", "nearest"]].to_string())
                else:
                    self.assertIn("uppercut_pullback", session.name)
                    self.assertTrue((accepted.nearest == "uppercut").all(),
                                    accepted[["t0", "kind", "nearest"]].to_string())
                    self.assertGreater(len(accepted), 0)


if __name__ == "__main__":
    unittest.main()
