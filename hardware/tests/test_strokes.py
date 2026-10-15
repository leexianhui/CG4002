"""Stroke + template valid-action check (figpipe/raw.py, figpipe/replay.py,
fit_strike_model.py) on synthetic IMU data — none of it comes from captures.

    python -m unittest discover -s tests -t .
"""

import os
import tempfile
import unittest

import numpy as np
import pandas as pd

import fit_strike_model as F
from figpipe import parsers as P
from figpipe import raw
from figpipe import replay as R

DT = 10  # ms, 100 Hz like the hub
A_STRIKE = np.array([0.6, 0.0, -0.8])     # strike rotation axis
A_FLICK = np.array([0.0, 1.0, 0.0])       # unrelated axis


def pulse(n, peak, axis):
    """Smooth half-sine rotation pulse: n samples, peak dps about axis."""
    w = peak * np.sin(np.linspace(0, np.pi, n))
    return w[:, None] * axis[None, :]


def thrust_lin(n_half, peak_g, axis=np.array([0.0, 1.0, 0.0])):
    """Linear accel (g) of a push: half-sine forward then half-sine back to rest."""
    s = peak_g * np.sin(np.linspace(0, np.pi, n_half))
    return np.concatenate([s, -s])[:, None] * axis[None, :]


def session(segments, name="synthetic_vertical_normal", label="vertical", speed="normal"):
    """Hub + torso frames from [(still_samples, gyro_pulse_or_None[, lin_accel])]:
    gravity on +z plus any linear accel (g). A segment's gyro pulse and linear
    accel start together; the shorter is padded with zeros."""
    G, L = [], []
    for seg in segments:
        still, p = seg[0], seg[1]
        lin = seg[2] if len(seg) > 2 else None
        G.append(np.zeros((still, 3)))
        L.append(np.zeros((still, 3)))
        n = max(len(p) if p is not None else 0, len(lin) if lin is not None else 0)
        g, l = np.zeros((n, 3)), np.zeros((n, 3))
        if p is not None:
            g[:len(p)] = p
        if lin is not None:
            l[:len(lin)] = lin
        G.append(g)
        L.append(l)
    G = np.vstack(G + [np.zeros((80, 3))])
    L = np.vstack(L + [np.zeros((80, 3))])
    n = len(G)
    t = np.arange(n) * DT + 1000
    A = np.tile([0.0, 0.0, 1.0], (n, 1)) + L
    t0 = pd.Timestamp("2026-10-04 12:00:00")
    hub = pd.DataFrame({"t": t, "ax": A[:, 0], "ay": A[:, 1], "az": A[:, 2],
                        "gx": G[:, 0], "gy": G[:, 1], "gz": G[:, 2], "seq": np.arange(n) // 10})
    hub["gyroMag"] = np.linalg.norm(G, axis=1)
    hub["jerk"] = np.abs(np.linalg.norm(A, axis=1) - 1.0)
    hub["rx"] = [t0 + pd.Timedelta(milliseconds=int(v)) for v in t]
    torso = hub[["t", "seq", "rx"]].copy()
    torso["gyroMag"] = 5.0  # a little torso movement, beacons present
    return raw.Session(name=name, label=label, speed=speed, cls="real", hub=hub, torso=torso,
                       lost_batches={"right_wrist": 0, "torso": 0})


def model_from_stroke(row):
    """One-template model centred on a stroke's own features."""
    return pd.DataFrame([{"action": "vertical", "ux": row.ux, "uy": row.uy, "uz": row.uz, "axis_spread": 15.0,
                          "px": row.hx, "py": row.hy, "pz": row.hz, "push_spread": 30.0,
                          "rot_mean": row.rot, "rot_spread": 0.2 * row.rot, "torso_ref": 0.2,
                          "torso_spread": 0.2, "bidirectional": False}])


class TestStrokes(unittest.TestCase):
    def test_find_strokes_axis_and_angle(self):
        s = session([(50, pulse(60, 400, A_STRIKE))])
        st = raw.find_strokes(s.hub)
        self.assertEqual(len(st), 1)
        a, b, ip, u = st[0]
        self.assertGreater(abs(u @ A_STRIKE), 0.99)
        # half-sine: integral = peak * duration * 2/pi = 400 * 0.6 s * 0.637 ~ 153 deg
        rot = np.trapezoid(s.hub[["gx", "gy", "gz"]].to_numpy()[a:b + 1] @ u, s.hub.t.to_numpy()[a:b + 1] / 1000)
        self.assertAlmostEqual(rot, 400 * 0.59 * 2 / np.pi, delta=12)

    def test_tremor_is_not_a_stroke(self):
        s = session([(50, pulse(60, 60, A_STRIKE))])  # below STROKE_MIN_PEAK_DPS
        self.assertEqual(raw.find_strokes(s.hub), [])


def rot_only(rep):
    """Rotation strokes. A rotating synthetic board with fixed accel also
    produces (unphysical) push strokes; the rotation tests ignore them."""
    return rep[rep.kind == "rot"]


class TestDecision(unittest.TestCase):
    def setUp(self):
        # strike, then its return straight after, then (later) a flick on another axis
        self.sess = session([(50, pulse(60, 400, A_STRIKE)), (5, pulse(70, 250, -A_STRIKE)),
                             (150, pulse(40, 300, A_FLICK))])
        self.rep = rot_only(R.replay_session(self.sess))
        self.model = model_from_stroke(self.rep.iloc[0])

    def test_strike_return_flick(self):
        self.assertEqual(len(self.rep), 3)
        dec = R.decide(self.rep, self.model, 1.75)
        self.assertEqual(list(dec.reason), ["ok", "return", "far"])

    def test_same_strike_slower_is_accepted(self):
        # half the speed, twice as long: same axis, same rotation angle
        slow = rot_only(R.replay_session(session([(50, pulse(120, 200, A_STRIKE))])))
        self.assertEqual(len(slow), 1)
        self.assertAlmostEqual(slow.rot.iat[0], self.rep.rot.iat[0], delta=0.05 * self.rep.rot.iat[0])
        self.assertTrue(R.decide(slow, self.model, 1.75).accept.iat[0])

    def test_return_window_scales_with_strike(self):
        # the same opposite stroke long after the strike is NOT a return
        late = rot_only(R.replay_session(session([(50, pulse(60, 400, A_STRIKE)), (300, pulse(70, 250, -A_STRIKE))])))
        dec = R.decide(late, self.model, 1.75)
        self.assertEqual(dec.reason.iat[1], "far")

    def test_no_torso_data(self):
        rep = self.rep.copy()
        rep.loc[rep.index[0], "torso_beacons"] = 0
        self.assertEqual(R.decide(rep, self.model, 1.75).reason.iat[0], "torso_data")

    def test_vectorised_distance_matches_scalar(self):
        d = R.distances(self.rep, self.model)
        for i, r in self.rep.iterrows():
            near, dist, *_ = R.template_distance(np.array([r.ux, r.uy, r.uz]), np.array([r.hx, r.hy, r.hz]),
                                                 r.rot, r.torso_ratio, self.model)
            self.assertAlmostEqual(d.dist[i], dist, places=6)

    def test_fast_uppercut_needs_torso_motion_but_slow_one_does_not(self):
        row = self.rep.iloc[[0]].copy().reset_index(drop=True)
        model = self.model.copy()
        model.loc[0, "action"] = "uppercut"
        row.loc[0, "torso_ratio"] = 0.02
        self.assertEqual(R.decide(row, model, 1.75).reason.iat[0], "torso_quiet")
        row.loc[0, "torso_ratio"] = 0.08
        self.assertEqual(R.decide(row, model, 1.75).reason.iat[0], "ok")
        row.loc[0, "torso_ratio"] = 0.02
        row.loc[0, "peak"] = 200.0
        self.assertEqual(R.decide(row, model, 1.75).reason.iat[0], "ok")


def thrust_model_from_push(row):
    """Thrust template centred on a push stroke's own features."""
    return {"action": R.THRUST_ACTION, "ux": row.ux, "uy": row.uy, "uz": row.uz, "dir_spread": 20.0,
            "reach_mean": row.reach, "reach_spread": 0.3, "rot_ref": 40.0, "rot_spread": 10.0}


class TestThrust(unittest.TestCase):
    """A thrust with no wrist rotation is found from linear motion alone."""

    def setUp(self):
        self.rep = R.replay_session(session([(50, None, thrust_lin(25, 2.0))], label="thrust"))
        self.push = self.rep[self.rep.kind == "push"]
        # the forward (+y) phase; the stopping phase is a push along -y
        cos_y = self.push.uy.to_numpy(float)
        self.fwd = self.push.iloc[int(np.argmax(cos_y))]

    def test_pure_push_is_found(self):
        self.assertEqual(len(self.rep[self.rep.kind == "rot"]), 0)  # no rotation stroke at all
        fwd = self.fwd
        self.assertGreater(np.array([fwd.ux, fwd.uy, fwd.uz]) @ np.array([0, 1, 0]), 0.99)
        self.assertLess(fwd.rot, 1.0)
        # true travel of the profile: v and x integrated from the half-sines
        lin = thrust_lin(25, 2.0)[:, 1] * R.G0
        v = np.cumsum(lin) * DT / 1000
        true_reach = np.sum(np.clip(v, 0, None)) * DT / 1000
        self.assertAlmostEqual(fwd.reach, true_reach, delta=0.35 * true_reach)

    def test_thrust_accepted_without_rotation(self):
        fwd = self.fwd
        dec = R.decide(self.rep, pd.DataFrame(columns=["action"]), 1.5, thrust=thrust_model_from_push(fwd),
                       thrust_max=1.0)
        self.assertTrue(dec.loc[dec.t0 == fwd.t0, "accept"].iat[0])

    def test_short_push_is_rejected_even_when_it_matches_direction(self):
        short = R.replay_session(session([(50, None, thrust_lin(25, 1.0))], label="thrust"))
        fwd = short[(short.kind == "push") & (short.uy > 0.5)].iloc[0]
        dec = R.decide(short, pd.DataFrame(columns=["action"]), 1.5,
                       thrust=thrust_model_from_push(fwd), thrust_max=1.0)
        self.assertEqual(dec.loc[dec.t0 == fwd.t0, "reason"].iat[0], "short")

    def test_immediate_second_push_is_return_even_if_direction_matches(self):
        fwd = self.fwd
        second = fwd.copy()
        second["t0"] = fwd.t1 + 200
        second["t1"] = second.t0 + fwd.dur
        second["cand"] = fwd.cand + 1
        rep = pd.DataFrame([fwd, second]).reset_index(drop=True)
        dec = R.decide(rep, pd.DataFrame(columns=["action"]), 1.5,
                       thrust=thrust_model_from_push(fwd), thrust_max=1.0)
        self.assertEqual(list(dec.reason), ["ok", "return"])

    def test_lead_in_to_a_swing_is_not_a_thrust(self):
        # the same push, with a strike rotation in the same candidate right after
        rep = R.replay_session(session([(50, None, thrust_lin(25, 1.0)), (0, pulse(60, 400, A_STRIKE))]))
        rot = rep[rep.kind == "rot"]
        strike = rot.loc[rot.peak.idxmax()]
        fwd = self.fwd
        dec = R.decide(rep, model_from_stroke(strike), 1.75, thrust=thrust_model_from_push(fwd), thrust_max=5.0)
        self.assertTrue(dec.loc[(dec.kind == "rot") & (dec.peak == strike.peak), "accept"].iat[0])
        lead = dec[(dec.kind == "push") & (dec.t1 <= strike.t0 + 1)]
        self.assertTrue(len(lead))
        self.assertFalse(lead.accept.any())
        self.assertTrue(set(lead.reason) <= {"lead_in", "overlap", "far"})


class TestModelRoundTrip(unittest.TestCase):
    def test_c_block_parses_back(self):
        rep = rot_only(R.replay_session(session([(50, pulse(60, 400, A_STRIKE))])))
        model = model_from_stroke(rep.iloc[0])
        thrust = {"ux": -0.24, "uy": 0.96, "uz": -0.14, "dir_spread": 36.3, "reach_mean": 0.61,
                  "reach_spread": 0.485, "rot_ref": 50.6, "rot_spread": 12.6}
        block = F.c_block(model, 1.75, thrust, 1.25, ["20261004_120000_synthetic"])
        src = "header\n" + block + "\nfooter\n"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sketch.ino")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(src)
            back, limit, back_thrust, thrust_limit = R.model_from_sketch(p)
        self.assertEqual((limit, thrust_limit), (1.75, 1.25))
        for c in R.MODEL_COLS:
            self.assertAlmostEqual(back[c].iat[0], model[c].iat[0], places=3)
        for c in R.THRUST_COLS:
            self.assertAlmostEqual(back_thrust[c], thrust[c], places=3)
        self.assertTrue(F.BLOCK_RE.search(src))


# Built from the onMotionEnd() format strings in "2 players/p1_right_wrist.ino".
SAMPLE_STROKE = ("[wrist] Stroke i=0 t0=443617 t1=444420 peak=412.3 rot=198.5 ax=0.483 ay=-0.053 az=-0.874 "
                 "px=0.011 py=0.987 pz=-0.162 torsoRot=12.4 torsoRatio=0.071 beacons=55 nearest=vertical "
                 "dist=0.62 dAxis=0.31 dPush=0.40 dRot=0.12 dTorso=0.00 reason=ok accept=1")
SAMPLE_PUSH = ("[wrist] Stroke i=0 kind=push t0=408626 t1=409098 peak=2.97 rot=51.6 reach=0.880 ax=-0.241 "
               "ay=0.957 az=-0.163 beacons=24 nearest=thrust dist=0.81 dAxis=0.30 dReach=0.56 dRot=0.08 "
               "reason=ok accept=1")
SAMPLE_CAND = ("[wrist] Candidate ENDED weapon=sword_shield raw=152 rawDur=1510 strokes=2 pushes=3 accepted=1 "
               "reason=settled")


class TestStrokeLines(unittest.TestCase):
    def test_stroke(self):
        d = P.parse_line(SAMPLE_STROKE)
        self.assertEqual((d["kind"], d["node"]), ("stroke", "right_wrist"))
        self.assertEqual((d["i"], d["t0"], d["nearest"], d["reason"], d["accept"]), (0, 443617, "vertical", "ok", 1))
        self.assertAlmostEqual(d["dist"], 0.62)
        self.assertEqual((d["stroke_kind"], d["kind_logged"]), ("rot", False))  # line from before push strokes

    def test_push_stroke(self):
        d = P.parse_line(SAMPLE_PUSH)
        self.assertEqual((d["kind"], d["stroke_kind"], d["kind_logged"]), ("stroke", "push", True))
        self.assertAlmostEqual(d["reach"], 0.88)

    def test_stroke_missing_key(self):
        with self.assertRaises(ValueError):
            P.parse_stroke(SAMPLE_STROKE.replace(" dist=0.62", ""))

    def test_candidate_ended(self):
        d = P.parse_line(SAMPLE_CAND)
        self.assertEqual((d["kind"], d["strokes"], d["accepted"], d["reason"]), ("candidate_ended", 2, 1, "settled"))


if __name__ == "__main__":
    unittest.main()
