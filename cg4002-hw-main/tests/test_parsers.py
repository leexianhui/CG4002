"""Each parser against sample lines in the firmware's exact format.

Every line is made up, built from the debugLog()/resultLog() format strings
in combined/*.ino; none come from captured logs.

    python -m unittest discover -s tests -t .
"""

import unittest

from figpipe import parsers as P

# A wrist-only flick: torsoPeak below MIN_TORSO_ENGAGEMENT, so the engagement gate fails.
SAMPLE_ENDED = ("[wrist] Motion ENDED. weapon=spear raw=100 rawDur=990 trimmed=[40,88] peak=250.0 dur=480 "
                "totalRot=60.0 rawTotalRot=80.0 peakAccel=1.50 axes=2 sustainFrac=0.40 reversals=1 rampMs=200 "
                "magOK=1 shapeOK=1 spinLikely=0 thrustLikely=0 swingShapeOK=1 spinReversalOK=1 torsoPeak=5.0 "
                "torsoDataOK=1 torsoCeilingOK=1 torsoEngagementOK=0 torsoOK=0 rawDurationOK=1")
SAMPLE_REJECTED = ("[wrist] REJECTED — torsoPeak=5.0 below 20.0 min (wrist-only motion, no body engagement "
                   "— likely a flick)")
SAMPLE_STATS = "[torso] [stats] to=torso from=right_wrist type=HUB_STATUS rx=9 lost=1"
SAMPLE_CFG = "[wrist] [cfg] wifi_sleep=off latency_test=0 player=1"
SAMPLE_CLOSING = "[wrist] [candidate] closing: settled"


class TestPrefix(unittest.TestCase):
    def test_tags(self):
        self.assertEqual(P.split_prefix("[left_wrist] raw el=1.0"), ("left_wrist", "raw el=1.0"))
        self.assertEqual(P.split_prefix("no tag here"), (None, "no tag here"))

    def test_node_mapping(self):
        self.assertEqual(P.parse_line(SAMPLE_CFG)["node"], "right_wrist")
        self.assertEqual(P.parse_line("[right_wrist] all_nodes_ready")["node"], "right_wrist")
        self.assertEqual(P.parse_line(SAMPLE_STATS)["node"], "torso")


class TestMotion(unittest.TestCase):
    def test_ended(self):
        d = P.parse_motion_ended(SAMPLE_ENDED)
        self.assertEqual(d["weapon"], "spear")
        self.assertEqual((d["trimStart"], d["trimEnd"]), (40, 88))
        self.assertEqual(d["raw"], 100)
        self.assertAlmostEqual(d["peak"], 250.0)
        self.assertAlmostEqual(d["sustainFrac"], 0.40)
        self.assertEqual(d["torsoEngagementOK"], 0)
        for k in P.ENDED_KEYS:
            if k != "trimmed":
                self.assertIn(k, d)

    def test_ended_sent_window(self):
        self.assertNotIn("sentStart", P.parse_motion_ended(SAMPLE_ENDED))  # older firmware: no sent=
        spin = SAMPLE_ENDED.replace("trimmed=[40,88]", "trimmed=[40,88] sent=[0,99]")
        d = P.parse_motion_ended(spin)
        self.assertEqual((d["sentStart"], d["sentEnd"]), (0, 99))
        self.assertNotIn("sent", d)

    def test_ended_missing_field_raises(self):
        with self.assertRaises(ValueError):
            P.parse_motion_ended("[wrist] Motion ENDED. weapon=spear raw=10")

    def test_started(self):
        self.assertEqual(P.parse_motion_started("[wrist] Motion STARTED at 630802"), {"t": 630802})

    def test_closing(self):
        self.assertEqual(P.parse_candidate_closing(SAMPLE_CLOSING), {"reason": "settled"})
        self.assertEqual(P.parse_candidate_closing("[wrist] [candidate] closing: bufferFull")["reason"],
                         "bufferFull")

    def test_rejected(self):
        self.assertTrue(P.parse_rejected(SAMPLE_REJECTED)["text"].startswith("torsoPeak=5.0"))
        self.assertEqual(P.parse_rejected("[wrist] REJECTED")["text"], "")


class TestSamples(unittest.TestCase):
    def test_seg_in_trim(self):
        d = P.parse_seg("[wrist] [seg] i=12* t=630912 ax=0.123 ay=-0.456 az=0.789 gx=12.3 gy=-45.6 gz=78.9 "
                        "gyroMag=91.9 jerk=0.12")
        self.assertEqual((d["i"], d["inTrim"], d["t"]), (12, True, 630912))
        self.assertAlmostEqual(d["gy"], -45.6)
        self.assertAlmostEqual(d["jerk"], 0.12)

    def test_seg_outside_trim(self):
        d = P.parse_seg("[wrist] [seg] i=3  t=630800 ax=0.000 ay=0.000 az=1.000 gx=0.0 gy=0.0 gz=0.0 "
                        "gyroMag=0.0 jerk=0.00")
        self.assertFalse(d["inTrim"])

    def test_seg_torso(self):  # torso dump has no trim mark
        d = P.parse_line("[torso] [seg] i=0 t=100 ax=0.000 ay=0.000 az=1.000 gx=1.0 gy=2.0 gz=3.0 "
                         "gyroMag=3.7 jerk=0.00")
        self.assertEqual((d["kind"], d["node"], d["inTrim"]), ("seg", "torso", False))

    def test_raw_sample(self):
        d = P.parse_raw_sample("[wrist] [raw] t=5000 ax=0.010 ay=0.020 az=0.990 gx=1.0 gy=2.0 gz=3.0 "
                               "gyroMag=3.7 jerk=0.01 state=IDLE")
        self.assertEqual((d["t"], d["state"]), (5000, "IDLE"))
        d = P.parse_raw_sample("[torso] [raw] t=5 ax=0.010 ay=0.020 az=0.990 gx=1.0 gy=2.0 gz=3.0 "
                               "gyroMag=3.7 jerk=0.01")
        self.assertNotIn("state", d)


class TestPose(unittest.TestCase):
    HUB = ("[wrist] raw rEl=-14.2 rAz=6.1 rGyroMag=22.5 rStable=1 | left el=49.8 az=-15.0 fresh=1 stable=1 | "
           "torso pitch=2.0 roll=-1.5 fresh=1 stable=1 | nearest=spear dist=3.4 candidate=spear heldMs=2100")

    def test_hub_pose_full(self):
        d = P.parse_hub_pose(self.HUB)
        self.assertAlmostEqual(d["rEl"], -14.2)
        self.assertAlmostEqual(d["l_el"], 49.8)
        self.assertEqual(d["t_fresh"], 1)
        self.assertEqual((d["nearest"], d["dist"], d["candidate"], d["heldMs"]), ("spear", 3.4, "spear", 2100))

    def test_hub_pose_unknown(self):
        d = P.parse_hub_pose("[wrist] raw rEl=0.0 rAz=0.0 rGyroMag=300.0 rStable=0 | left el=0.0 az=0.0 fresh=0 "
                             "stable=0 | torso pitch=0.0 roll=0.0 fresh=0 stable=0 | nearest=unknown "
                             "candidate=unknown")
        self.assertIsNone(d["dist"])
        self.assertIsNone(d["heldMs"])

    def test_old_pose_format_not_matched(self):  # pre el/az firmware format, made-up values
        self.assertIsNone(P.parse_hub_pose("[wrist] raw rPitch=1.0 rRoll=2.0 rGyroMag=10.0 rStable=0 | left "
                                           "pitch=0.0 roll=0.0 fresh=0 stable=0 | torso pitch=0.0 roll=0.0 "
                                           "fresh=0 stable=0 | candidate=unknown"))

    def test_left_raw(self):
        d = P.parse_left_raw("[left_wrist] raw el=50.1 az=-14.7 gyroMag=31.0 stable=1 gx=1.2 gy=-3.4 gz=0.5 "
                             "imuFails=0")
        self.assertEqual((d["el"], d["gyroMag"], d["imuFails"]), (50.1, 31.0, 0))

    def test_torso_pose(self):
        d = P.parse_torso_pose("[torso] raw pitch=2.0 roll=-1.5 gyroMag=4.2 stable=1")
        self.assertEqual((d["pitch"], d["gyroMag"], d["stable"]), (2.0, 4.2, 1))


class TestResults(unittest.TestCase):
    def test_candidate(self):
        d = P.parse_result("[right_wrist] weapon_candidate=mace dist=12.3 rEl=2.0 rAz=-66.0 lEl=-22.0 lAz=-50.0")
        self.assertEqual((d["event"], d["weapon"], d["dist"], d["lAz"]), ("weapon_candidate", "mace", 12.3, -50.0))

    def test_candidate_lost(self):
        d = P.parse_result("[right_wrist] weapon_candidate_lost (was spear heldMs=3200 reason=r_unstable,l_stale "
                           "nearest=spear dist=25.1 rEl=-10.0 rAz=4.0 lEl=40.0 lAz=-10.0)")
        self.assertEqual((d["was"], d["heldMs"], d["reason"], d["lAz"]),
                         ("spear", 3200, "r_unstable,l_stale", -10.0))

    def test_locked_ready_accepted(self):
        self.assertEqual(P.parse_result("[right_wrist] weapon_locked=sword_shield"),
                         {"event": "weapon_locked", "weapon": "sword_shield"})
        self.assertEqual(P.parse_result("[right_wrist] all_nodes_ready"), {"event": "all_nodes_ready"})
        d = P.parse_result("[right_wrist] action_accepted weapon=spear seg=726459")
        self.assertEqual((d["event"], d["seg"]), ("action_accepted", 726459))

    def test_lock_ack(self):
        d = P.parse_line("[torso] Weapon locked by hub: weapon=2 — switching to beacon mode")
        self.assertEqual((d["kind"], d["weaponId"]), ("lock_ack", 2))


class TestNetwork(unittest.TestCase):
    def test_stats(self):
        self.assertEqual(P.parse_stats(SAMPLE_STATS),
                         {"to": "torso", "from": "right_wrist", "type": "HUB_STATUS", "rx": 9, "lost": 1})

    def test_rtt(self):
        self.assertEqual(P.parse_rtt("[wrist] [rtt] us=4210"), {"us": 4210})

    def test_iat(self):
        self.assertEqual(P.parse_beacon_iat("[wrist] [beacon_iat] us=20011,19987,45000")["us"],
                         [20011, 19987, 45000])

    def test_cfg(self):
        self.assertEqual(P.parse_cfg(SAMPLE_CFG), {"wifi_sleep": "off", "latency_test": 0, "player": 1})
        # left wrist omits latency_test
        self.assertEqual(P.parse_cfg("[left_wrist] [cfg] wifi_sleep=on player=1"), {"wifi_sleep": "on", "player": 1})


class TestDispatch(unittest.TestCase):
    def test_every_kind_reachable(self):
        samples = {
            "motion_ended": SAMPLE_ENDED, "rejected": SAMPLE_REJECTED, "stats": SAMPLE_STATS, "cfg": SAMPLE_CFG,
            "candidate_closing": SAMPLE_CLOSING, "motion_started": "[wrist] Motion STARTED at 1",
            "hub_pose": TestPose.HUB, "rtt": "[wrist] [rtt] us=1",
            "result": "[right_wrist] all_nodes_ready",
        }
        for kind, line in samples.items():
            self.assertEqual(P.parse_line(line)["kind"], kind, line)

    def test_unused_lines_ignored(self):
        self.assertIsNone(P.parse_line("[wrist] Sent SegmentRequest seg=726459"))
        self.assertIsNone(P.parse_line("[left_wrist] [cal] gyro offset x=1.4 y=-2.8 z=-0.3 dps, good reads=500/500"))


if __name__ == "__main__":
    unittest.main()
