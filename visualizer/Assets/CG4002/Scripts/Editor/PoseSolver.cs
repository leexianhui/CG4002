using System.Collections.Generic;
using UnityEditor;
using UnityEngine;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Arm pose as directions in avatar space (+Z forward, +X right) rather than joint angles.
    /// "facing" is the direction the front of the forearm points: for the weapon hand that is
    /// the direction the weapon points, for the shield arm it is the direction the shield faces.
    /// </summary>
    struct ArmPose
    {
        public Vector3 upper;
        public Vector3 fore;
        public Vector3 facing;

        public ArmPose(Vector3 upper, Vector3 fore, Vector3 facing)
        {
            this.upper = upper;
            this.fore = fore;
            this.facing = facing;
        }
    }

    /// <summary>
    /// A full-body key pose. Torso is authored as angles, arms as directions, and legs as
    /// foot positions solved with two-bone IK so feet stay planted while the hips move.
    /// </summary>
    class Pose
    {
        public Vector3 hips;  // offset from the rest hip position
        public float hipsYaw, spineYaw, spinePitch, chestYaw, chestPitch, chestRoll, headPitch;
        /// <summary>
        /// Turns the whole pose (hips, arms, feet) about the vertical axis, e.g. for a spin.
        /// Unlike hipsYaw, arm directions and feet turn with it, and the head doesn't counter-rotate.
        /// </summary>
        public float rootYaw;
        public Vector3 footL = new(-0.14f, MannequinRig.AnkleHeight, 0.1f);
        public Vector3 footR = new(0.15f, MannequinRig.AnkleHeight, -0.08f);
        public ArmPose armR = new(new Vector3(0.15f, -1f, 0.05f), new Vector3(0.1f, -1f, 0.2f), Vector3.forward);
        public ArmPose armL = new(new Vector3(-0.15f, -1f, 0.05f), new Vector3(-0.1f, -1f, 0.2f), Vector3.forward);

        public Pose Copy() => (Pose)MemberwiseClone();

        public static Pose Lerp(Pose a, Pose b, float t)
        {
            return new Pose
            {
                hips = Vector3.Lerp(a.hips, b.hips, t),
                hipsYaw = Mathf.Lerp(a.hipsYaw, b.hipsYaw, t),
                spineYaw = Mathf.Lerp(a.spineYaw, b.spineYaw, t),
                spinePitch = Mathf.Lerp(a.spinePitch, b.spinePitch, t),
                chestYaw = Mathf.Lerp(a.chestYaw, b.chestYaw, t),
                chestPitch = Mathf.Lerp(a.chestPitch, b.chestPitch, t),
                chestRoll = Mathf.Lerp(a.chestRoll, b.chestRoll, t),
                headPitch = Mathf.Lerp(a.headPitch, b.headPitch, t),
                rootYaw = Mathf.Lerp(a.rootYaw, b.rootYaw, t),
                footL = Step(a.footL, b.footL, t),
                footR = Step(a.footR, b.footR, t),
                armR = Lerp(a.armR, b.armR, t),
                armL = Lerp(a.armL, b.armL, t),
            };
        }

        static ArmPose Lerp(ArmPose a, ArmPose b, float t) => new(
            Vector3.Slerp(a.upper.normalized, b.upper.normalized, t),
            Vector3.Slerp(a.fore.normalized, b.fore.normalized, t),
            Vector3.Slerp(a.facing.normalized, b.facing.normalized, t));

        /// <summary>Slide the foot to its new spot, lifting it in an arc proportional to the distance.</summary>
        static Vector3 Step(Vector3 from, Vector3 to, float t)
        {
            Vector3 foot = Vector3.Lerp(from, to, t);
            foot.y += Mathf.Sin(t * Mathf.PI) * Vector3.Distance(from, to) * 0.4f;
            return foot;
        }
    }

    enum Ease
    {
        Linear,
        Smooth,
        /// <summary>Accelerate into the key. Use when arriving at an impact.</summary>
        In,
        /// <summary>Decelerate into the key. Use for wind-ups and follow-throughs.</summary>
        Out,
    }

    class ClipSpec
    {
        public string name;
        public bool loop;
        public bool guardLayerOnly;
        public readonly List<(float time, Pose pose, Ease ease)> keys = new();
        public readonly List<(float time, string function)> events = new();

        public ClipSpec Key(float time, Pose pose, Ease ease = Ease.Smooth)
        {
            keys.Add((time, pose, ease));
            return this;
        }

        public ClipSpec Event(float time, string function)
        {
            events.Add((time, function));
            return this;
        }

        public float Length => keys[keys.Count - 1].time;

        public Pose Evaluate(float time)
        {
            if (time <= keys[0].time)
                return keys[0].pose;
            for (int i = 1; i < keys.Count; i++)
            {
                if (time > keys[i].time)
                    continue;
                var (t0, p0, _) = keys[i - 1];
                var (t1, p1, ease) = keys[i];
                float u = Mathf.InverseLerp(t0, t1, time);
                return Pose.Lerp(p0, p1, Apply(ease, u));
            }
            return keys[keys.Count - 1].pose;
        }

        static float Apply(Ease ease, float u) => ease switch
        {
            Ease.Smooth => u * u * (3f - 2f * u),
            Ease.In => u * u * u,
            Ease.Out => 1f - (1f - u) * (1f - u) * (1f - u),
            _ => u,
        };
    }

    static class PoseSolver
    {
        const float SampleRate = 60f;

        public struct Solved
        {
            public Dictionary<string, Quaternion> localRotations;
            public Vector3 hipsLocalPosition;
        }

        public static Solved Solve(Pose pose)
        {
            var local = new Dictionary<string, Quaternion>();

            Quaternion root = Quaternion.Euler(0f, pose.rootYaw, 0f);
            Quaternion hipsW = root * Quaternion.Euler(0f, pose.hipsYaw, 0f);
            local[MannequinRig.Hips] = hipsW;

            Quaternion spineL = Quaternion.Euler(pose.spinePitch, pose.spineYaw, 0f);
            Quaternion spineW = hipsW * spineL;
            local[MannequinRig.Spine] = spineL;

            Quaternion chestL = Quaternion.Euler(pose.chestPitch, pose.chestYaw, pose.chestRoll);
            Quaternion chestW = spineW * chestL;
            local[MannequinRig.Chest] = chestL;

            // Head counter-rotates most of the torso twist so the avatar keeps looking at its opponent.
            float totalYaw = pose.hipsYaw + pose.spineYaw + pose.chestYaw;
            float totalPitch = pose.spinePitch + pose.chestPitch;
            local[MannequinRig.Head] = Quaternion.Euler(pose.headPitch - totalPitch * 0.6f, -totalYaw * 0.75f, -pose.chestRoll * 0.5f);

            SolveArm(Turn(root, pose.armR), chestW, MannequinRig.UpperArmR, MannequinRig.ForearmR, local);
            SolveArm(Turn(root, pose.armL), chestW, MannequinRig.UpperArmL, MannequinRig.ForearmL, local);

            Vector3 hipsPos = MannequinRig.HipsRest + root * pose.hips;
            Quaternion feetW = root * Quaternion.Euler(0f, pose.hipsYaw * 0.5f, 0f);
            SolveLeg(hipsPos, hipsW, +1f, root * pose.footR, MannequinRig.UpperLegR, MannequinRig.LowerLegR, MannequinRig.FootR, feetW, local);
            SolveLeg(hipsPos, hipsW, -1f, root * pose.footL, MannequinRig.UpperLegL, MannequinRig.LowerLegL, MannequinRig.FootL, feetW, local);

            return new Solved { localRotations = local, hipsLocalPosition = hipsPos };
        }

        static ArmPose Turn(Quaternion root, ArmPose arm) => new(root * arm.upper, root * arm.fore, root * arm.facing);

        static void SolveArm(ArmPose arm, Quaternion chestW, string upperPath, string forePath, Dictionary<string, Quaternion> local)
        {
            // The upper arm's front faces the way the elbow bends, i.e. toward the forearm.
            Quaternion upperW = Aim(arm.upper, arm.fore, arm.facing);
            Quaternion foreW = Aim(arm.fore, arm.facing, Vector3.forward);
            local[upperPath] = Quaternion.Inverse(chestW) * upperW;
            local[forePath] = Quaternion.Inverse(upperW) * foreW;
        }

        static void SolveLeg(Vector3 hipsPos, Quaternion hipsW, float side, Vector3 foot,
            string thighPath, string shinPath, string footPath, Quaternion footW, Dictionary<string, Quaternion> local)
        {
            float a = MannequinRig.ThighLength;
            float b = MannequinRig.ShinLength;

            Vector3 hip = hipsPos + hipsW * MannequinRig.HipJoint(side);
            Vector3 toFoot = foot - hip;
            float distance = Mathf.Clamp(toFoot.magnitude, 0.05f, (a + b) * 0.999f);
            Vector3 direction = toFoot.normalized;

            // Knees bend forward and slightly outward.
            Vector3 pole = hipsW * new Vector3(0.15f * side, 0f, 1f);
            Vector3 bendAxis = (pole - Vector3.Dot(pole, direction) * direction).normalized;

            float cosHip = (a * a + distance * distance - b * b) / (2f * a * distance);
            float sinHip = Mathf.Sqrt(Mathf.Max(0f, 1f - cosHip * cosHip));
            Vector3 knee = hip + a * (cosHip * direction + sinHip * bendAxis);
            Vector3 ankle = hip + direction * distance;

            Quaternion thighW = Aim(knee - hip, pole, Vector3.forward);
            Quaternion shinW = Aim(ankle - knee, pole, Vector3.forward);

            local[thighPath] = Quaternion.Inverse(hipsW) * thighW;
            local[shinPath] = Quaternion.Inverse(thighW) * shinW;
            local[footPath] = Quaternion.Inverse(shinW) * footW;
        }

        /// <summary>
        /// Rotation that points a limb bone's -Y along <paramref name="along"/> with its +Z as
        /// close to <paramref name="front"/> as possible (falling back if the two are parallel).
        /// </summary>
        static Quaternion Aim(Vector3 along, Vector3 front, Vector3 fallback)
        {
            along.Normalize();
            Vector3 f = front - Vector3.Dot(front, along) * along;
            if (f.sqrMagnitude < 1e-3f)
                f = fallback - Vector3.Dot(fallback, along) * along;
            if (f.sqrMagnitude < 1e-3f)
                f = Vector3.up - Vector3.Dot(Vector3.up, along) * along;
            return Quaternion.LookRotation(f.normalized, -along);
        }

        // ---- Baking ------------------------------------------------------------------------------

        public static AnimationClip Bake(ClipSpec spec)
        {
            var clip = new AnimationClip { name = spec.name, frameRate = SampleRate };
            string[] bones = spec.guardLayerOnly ? MannequinRig.GuardBones : MannequinRig.AnimatedBones;

            int frames = Mathf.Max(2, Mathf.RoundToInt(spec.Length * SampleRate) + 1);
            var times = new float[frames];
            var rotations = new Dictionary<string, Quaternion[]>();
            foreach (var bone in bones)
                rotations[bone] = new Quaternion[frames];
            var hipsPositions = new Vector3[frames];

            for (int f = 0; f < frames; f++)
            {
                float time = Mathf.Min(f / SampleRate, spec.Length);
                times[f] = time;
                var solved = Solve(spec.Evaluate(time));
                foreach (var bone in bones)
                {
                    Quaternion q = solved.localRotations[bone];
                    // Keep neighbouring samples in the same hemisphere so interpolation takes the short way round.
                    if (f > 0 && Quaternion.Dot(q, rotations[bone][f - 1]) < 0f)
                        q = new Quaternion(-q.x, -q.y, -q.z, -q.w);
                    rotations[bone][f] = q;
                }
                hipsPositions[f] = solved.hipsLocalPosition;
            }

            foreach (var bone in bones)
            {
                var q = rotations[bone];
                SetCurve(clip, bone, "localRotation.x", times, i => q[i].x);
                SetCurve(clip, bone, "localRotation.y", times, i => q[i].y);
                SetCurve(clip, bone, "localRotation.z", times, i => q[i].z);
                SetCurve(clip, bone, "localRotation.w", times, i => q[i].w);
            }
            if (!spec.guardLayerOnly)
            {
                SetCurve(clip, MannequinRig.Hips, "localPosition.x", times, i => hipsPositions[i].x);
                SetCurve(clip, MannequinRig.Hips, "localPosition.y", times, i => hipsPositions[i].y);
                SetCurve(clip, MannequinRig.Hips, "localPosition.z", times, i => hipsPositions[i].z);
            }

            var settings = AnimationUtility.GetAnimationClipSettings(clip);
            settings.loopTime = spec.loop;
            AnimationUtility.SetAnimationClipSettings(clip, settings);

            var events = new List<AnimationEvent>();
            foreach (var (time, function) in spec.events)
                events.Add(new AnimationEvent { time = time, functionName = function });
            AnimationUtility.SetAnimationEvents(clip, events.ToArray());
            return clip;
        }

        static void SetCurve(AnimationClip clip, string path, string property, float[] times, System.Func<int, float> value)
        {
            int n = times.Length;
            var keys = new Keyframe[n];
            for (int i = 0; i < n; i++)
            {
                // Finite-difference tangents: flat tangents at 60 keys/s would make motion judder in slow motion.
                int prev = Mathf.Max(i - 1, 0);
                int next = Mathf.Min(i + 1, n - 1);
                float dt = times[next] - times[prev];
                float slope = dt > 0f ? (value(next) - value(prev)) / dt : 0f;
                keys[i] = new Keyframe(times[i], value(i), slope, slope);
            }
            clip.SetCurve(path, typeof(Transform), property, new AnimationCurve(keys));
        }
    }
}
