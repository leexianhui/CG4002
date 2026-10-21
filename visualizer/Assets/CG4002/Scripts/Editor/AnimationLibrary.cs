using System.Collections.Generic;
using CG4002.Combat;
using UnityEngine;
using C = CG4002.Models.AvatarContract;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Key poses for every weapon's clips. Directions are in avatar space:
    /// +X is the avatar's right, +Y up, +Z toward the opponent.
    ///
    /// Each weapon authors its own stance, guard and charged attack. The six classifier attacks
    /// (horizontal, vertical, diagonal, thrust, spin, uppercut) are shared motions played from
    /// that stance, at the weapon's pace.
    ///
    /// Timing note: the classifier labels an action only after the player's motion window
    /// closes, so these clips arrive late by design. Wind-ups are kept short and strikes
    /// front-loaded so the avatar catches up with the real opponent quickly.
    /// </summary>
    static class AnimationLibrary
    {
        public class WeaponClips
        {
            public ClipSpec idle, guard;
            public readonly Dictionary<AttackSlot, ClipSpec> attacks = new();
        }

        public static WeaponClips For(WeaponType weapon) => weapon switch
        {
            WeaponType.SwordAndShield => SwordAndShield(),
            WeaponType.Spear => Spear(),
            WeaponType.Mace => Mace(),
            _ => throw new System.ArgumentOutOfRangeException(nameof(weapon)),
        };

        static Vector3 V(float x, float y, float z) => new(x, y, z);

        static ClipSpec Idle(string name, Pose stance)
        {
            var inhale = stance.Copy();
            inhale.hips += V(0f, -0.012f, 0f);
            inhale.chestPitch += 2.5f;
            return new ClipSpec { name = name, loop = true }
                .Key(0f, stance)
                .Key(1.1f, inhale)
                .Key(2.2f, stance);
        }

        static ClipSpec Guard(string name, ArmPose raised)
        {
            var settle = new Pose { armL = raised };
            var shift = new Pose
            {
                armL = new ArmPose(raised.upper + V(0f, 0.04f, 0f), raised.fore + V(0f, -0.03f, 0f), raised.facing),
            };
            return new ClipSpec { name = name, loop = true, guardLayerOnly = true }
                .Key(0f, settle)
                .Key(0.6f, shift)
                .Key(1.2f, settle);
        }

        /// <summary>A weapon's clips: its own idle, guard and charged attack, plus every shared attack.</summary>
        static WeaponClips Complete(string prefix, Pose stance, float pace, ClipSpec charged, ClipSpec guard)
        {
            var clips = new WeaponClips { idle = Idle($"{prefix}_Idle", stance), guard = guard };
            clips.attacks[AttackSlot.Charged] = charged;
            clips.attacks[AttackSlot.Horizontal] = Horizontal($"{prefix}_Horizontal", stance, pace);
            clips.attacks[AttackSlot.Vertical] = Vertical($"{prefix}_Vertical", stance, pace);
            clips.attacks[AttackSlot.Diagonal] = Diagonal($"{prefix}_Diagonal", stance, pace);
            clips.attacks[AttackSlot.Thrust] = Thrust($"{prefix}_Thrust", stance, pace);
            clips.attacks[AttackSlot.Spin] = Spin($"{prefix}_Spin", stance, pace);
            clips.attacks[AttackSlot.Uppercut] = Uppercut($"{prefix}_Uppercut", stance, pace);
            return clips;
        }

        /// <summary>
        /// An attack starting from the stance. Key and event times are for a pace of 1 and are
        /// stretched by <paramref name="pace"/>; the last key should return to the stance.
        /// </summary>
        static ClipSpec Attack(string name, float pace, Pose stance, float open, float close, params (float time, Pose pose, Ease ease)[] keys)
        {
            var clip = new ClipSpec { name = name }.Key(0f, stance);
            foreach (var (time, pose, ease) in keys)
                clip.Key(time * pace, pose, ease);
            return clip.Event(open * pace, C.StrikeOpenEvent).Event(close * pace, C.StrikeCloseEvent);
        }

        /// <summary>Rolls an arm pose about the forward axis: positive raises the right side and lowers the left.</summary>
        static ArmPose Tilt(ArmPose arm, float degrees)
        {
            Quaternion roll = Quaternion.AngleAxis(degrees, Vector3.forward);
            return new ArmPose(roll * arm.upper, roll * arm.fore, roll * arm.facing);
        }

        // ---- Shared attacks ---------------------------------------------------------------------
        // One motion per classifier action, played from each weapon's stance with the weapon's model.
        // The weapon points along the forearm's "facing", perpendicular to the forearm.

        static readonly ArmPose CockedRight = new(V(0.9f, -0.1f, -0.3f), V(0.1f, -0.25f, 0.95f), V(0.55f, 0.35f, -0.75f));
        static readonly ArmPose CutAcross = new(V(0.3f, -0.15f, 0.95f), V(-0.95f, -0.1f, 0.2f), V(0.1f, 0f, 1f));
        static readonly ArmPose FollowLow = new(V(-0.35f, -0.35f, 0.85f), V(-0.2f, -0.9f, -0.3f), V(-0.8f, -0.1f, 0.5f));

        // Horizontal slash, right to left.
        static ClipSpec Horizontal(string name, Pose stance, float pace)
        {
            var cock = stance.Copy();
            cock.hips = V(0f, -0.05f, -0.03f);
            cock.hipsYaw = 25f;
            cock.chestYaw = 30f;
            cock.armR = CockedRight;

            var cut = stance.Copy();
            cut.hips = V(0f, -0.07f, 0.05f);
            cut.hipsYaw = -10f;
            cut.chestYaw = -25f;
            cut.chestPitch = 8f;
            cut.footL = V(-0.14f, MannequinRig.AnkleHeight, 0.2f);
            cut.armR = CutAcross;

            var follow = cut.Copy();
            follow.hipsYaw = -25f;
            follow.chestYaw = -40f;
            follow.armR = FollowLow;

            return Attack(name, pace, stance, open: 0.16f, close: 0.44f,
                (0.14f, cock, Ease.Out), (0.30f, cut, Ease.In), (0.42f, follow, Ease.Out), (0.62f, stance, Ease.Smooth));
        }

        // Vertical overhead chop.
        static ClipSpec Vertical(string name, Pose stance, float pace)
        {
            var raise = stance.Copy();
            raise.hips = V(0f, -0.05f, -0.04f);
            raise.hipsYaw = 10f;
            raise.chestYaw = 10f;
            raise.chestPitch = -8f;
            raise.armR = new ArmPose(V(0.35f, 0.9f, -0.1f), V(0f, 0.95f, -0.3f), V(0f, -0.3f, -0.95f));

            var smash = stance.Copy();
            smash.hips = V(0f, -0.1f, 0.08f);
            smash.chestYaw = -8f;
            smash.chestPitch = 22f;
            smash.footL = V(-0.14f, MannequinRig.AnkleHeight, 0.22f);
            smash.armR = new ArmPose(V(0.2f, -0.1f, 1f), V(0.05f, -0.9f, -0.4f), V(0f, -0.4f, 0.9f));

            var through = smash.Copy();
            through.armR = new ArmPose(V(0.2f, -0.5f, 0.85f), V(0f, -0.75f, -0.65f), V(0f, -0.65f, 0.75f));

            return Attack(name, pace, stance, open: 0.17f, close: 0.42f,
                (0.16f, raise, Ease.Out), (0.30f, smash, Ease.In), (0.42f, through, Ease.Out), (0.60f, stance, Ease.Smooth));
        }

        // Diagonal slash, high right to low left: the horizontal arc tilted 40 degrees.
        static ClipSpec Diagonal(string name, Pose stance, float pace)
        {
            var cock = stance.Copy();
            cock.hips = V(0f, -0.04f, -0.04f);
            cock.hipsYaw = 22f;
            cock.chestYaw = 28f;
            cock.chestPitch = -6f;
            cock.armR = Tilt(CockedRight, 40f);

            var cut = stance.Copy();
            cut.hips = V(0f, -0.1f, 0.06f);
            cut.hipsYaw = -8f;
            cut.chestYaw = -22f;
            cut.chestPitch = 16f;
            cut.footL = V(-0.14f, MannequinRig.AnkleHeight, 0.22f);
            cut.armR = Tilt(CutAcross, 40f);

            var follow = cut.Copy();
            follow.hipsYaw = -20f;
            follow.chestYaw = -35f;
            follow.chestPitch = 20f;
            follow.armR = Tilt(FollowLow, 40f);

            return Attack(name, pace, stance, open: 0.16f, close: 0.44f,
                (0.15f, cock, Ease.Out), (0.30f, cut, Ease.In), (0.42f, follow, Ease.Out), (0.62f, stance, Ease.Smooth));
        }

        // Straight thrust from a chambered elbow.
        static ClipSpec Thrust(string name, Pose stance, float pace)
        {
            var chamber = stance.Copy();
            chamber.hips = V(0f, -0.06f, -0.08f);
            chamber.hipsYaw = 30f;
            chamber.chestYaw = 20f;
            chamber.armR = new ArmPose(V(0.35f, -0.5f, -0.8f), V(0.1f, -0.95f, 0.2f), V(0f, 0.1f, 1f));

            var thrust = stance.Copy();
            thrust.hips = V(0f, -0.09f, 0.14f);
            thrust.hipsYaw = -5f;
            thrust.chestYaw = -15f;
            thrust.chestPitch = 10f;
            thrust.footL = V(-0.14f, MannequinRig.AnkleHeight, 0.32f);
            thrust.armR = new ArmPose(V(0.12f, -0.2f, 1f), V(0.05f, -1f, -0.05f), V(0f, 0.02f, 1f));

            var hold = thrust.Copy();
            hold.hips += V(0f, 0f, 0.02f);

            return Attack(name, pace, stance, open: 0.13f, close: 0.32f,
                (0.12f, chamber, Ease.Out), (0.24f, thrust, Ease.In), (0.34f, hold, Ease.Out), (0.55f, stance, Ease.Smooth));
        }

        // Spinning slash: wind up to the right, then a full turn to the left with the weapon held
        // out to the side, ending square to the opponent again.
        static ClipSpec Spin(string name, Pose stance, float pace)
        {
            var wind = stance.Copy();
            wind.hips = V(0f, -0.08f, 0f);
            wind.hipsYaw = 30f;
            wind.chestYaw = 35f;
            wind.armR = CockedRight;

            var outstretched = new ArmPose(V(0.6f, -0.2f, 0.75f), V(0.25f, -0.05f, 0.97f), V(0.97f, 0f, -0.25f));
            Pose Turned(float rootYaw)
            {
                var pose = stance.Copy();
                pose.hips = V(0f, -0.1f, 0f);
                pose.hipsYaw = -5f;
                pose.chestYaw = -10f;
                pose.chestPitch = 6f;
                pose.rootYaw = rootYaw;
                pose.armR = outstretched;
                return pose;
            }

            var finish = Turned(-360f);
            finish.chestYaw = -30f;
            finish.armR = FollowLow;

            // Stay at -360: unwinding to 0 would spin back the other way.
            var settle = stance.Copy();
            settle.rootYaw = -360f;

            return Attack(name, pace, stance, open: 0.16f, close: 0.50f,
                (0.14f, wind, Ease.Out),
                (0.24f, Turned(-90f), Ease.In),
                (0.32f, Turned(-180f), Ease.Linear),
                (0.40f, Turned(-270f), Ease.Linear),
                (0.50f, finish, Ease.Out),
                (0.72f, settle, Ease.Smooth));
        }

        // Uppercut: dip low with the weapon held forward at knee height, then drive it up in a
        // rising arc while straightening.
        static ClipSpec Uppercut(string name, Pose stance, float pace)
        {
            var dip = stance.Copy();
            dip.hips = V(0f, -0.15f, -0.02f);
            dip.hipsYaw = 18f;
            dip.chestYaw = 18f;
            dip.chestPitch = 22f;
            dip.footR = V(0.16f, MannequinRig.AnkleHeight, -0.14f);
            dip.armR = new ArmPose(V(0.3f, -0.95f, -0.15f), V(0.15f, -0.95f, 0.25f), V(0.05f, 0.25f, 1f));

            var rise = stance.Copy();
            rise.hips = V(0f, -0.04f, 0.08f);
            rise.hipsYaw = -5f;
            rise.chestYaw = -10f;
            rise.chestPitch = 0f;
            rise.footL = V(-0.14f, MannequinRig.AnkleHeight, 0.24f);
            rise.armR = new ArmPose(V(0.25f, -0.1f, 1f), V(0.05f, 0.3f, 0.95f), V(0f, 0.95f, -0.3f));

            var finish = rise.Copy();
            finish.hips = V(0f, 0f, 0.1f);
            finish.chestPitch = -12f;
            finish.chestYaw = -15f;
            finish.armR = new ArmPose(V(0.2f, 0.75f, 0.6f), V(0.05f, 0.5f, 0.85f), V(0f, 0.85f, -0.5f));

            return Attack(name, pace, stance, open: 0.15f, close: 0.42f,
                (0.14f, dip, Ease.Out), (0.28f, rise, Ease.In), (0.40f, finish, Ease.Out), (0.64f, stance, Ease.Smooth));
        }

        // ---- Sword and shield -------------------------------------------------------------------

        static WeaponClips SwordAndShield()
        {
            var stance = new Pose
            {
                hips = V(0f, -0.03f, 0f),
                hipsYaw = 10f,
                chestYaw = 6f,
                chestPitch = 4f,
                // The hub's sword ready stance (and the selection dummy's): sword pointing up,
                // left forearm across the chest with the shield in front.
                armR = new ArmPose(V(0.28f, -0.95f, 0.15f), V(0.1f, -0.3f, 1f), V(0f, 1f, 0.55f)),
                armL = new ArmPose(V(-0.2f, -0.75f, 0.6f), V(0.95f, 0.05f, 0.2f), V(0f, 0.1f, 1f)),
            };

            // Charged: raise overhead while glowing, then slam down.
            var raise = stance.Copy();
            raise.hips = V(0f, -0.1f, -0.06f);
            raise.hipsYaw = 15f;
            raise.chestYaw = 15f;
            raise.chestPitch = -12f;
            raise.footR = V(0.16f, MannequinRig.AnkleHeight, -0.16f);
            raise.armR = new ArmPose(V(0.3f, 0.95f, -0.1f), V(0.1f, 0.85f, -0.5f), V(0.05f, -0.5f, -0.85f));
            raise.armL = new ArmPose(V(-0.3f, -0.5f, 0.8f), V(0.4f, 0.5f, 0.75f), V(0f, 0f, 1f));

            var tremble = raise.Copy();
            tremble.hips += V(0f, -0.01f, 0f);
            tremble.chestPitch = -14f;

            var slam = stance.Copy();
            slam.hips = V(0f, -0.16f, 0.12f);
            slam.hipsYaw = -5f;
            slam.chestYaw = -10f;
            slam.chestPitch = 28f;
            slam.footL = V(-0.14f, MannequinRig.AnkleHeight, 0.32f);
            slam.armR = new ArmPose(V(0.15f, -0.4f, 0.9f), V(0f, -1f, -0.3f), V(0.05f, -0.3f, 1f));
            slam.armL = raise.armL;

            var settle = slam.Copy();
            settle.chestPitch = 32f;
            settle.armR = new ArmPose(V(0.15f, -0.6f, 0.8f), V(0f, -0.7f, -0.7f), V(0f, -0.7f, 0.7f));

            var charged = new ClipSpec { name = "Sword_Charged" }
                .Key(0f, stance)
                .Key(0.30f, raise, Ease.Out)
                .Key(0.38f, tremble, Ease.Linear)
                .Key(0.50f, slam, Ease.In)
                .Key(0.60f, settle, Ease.Out)
                .Key(0.95f, stance)
                .Event(0.04f, C.ChargeStartEvent)
                .Event(0.40f, C.StrikeOpenEvent)
                .Event(0.62f, C.StrikeCloseEvent);

            return Complete("Sword", stance, pace: 1f, charged,
                Guard("Sword_Guard", new ArmPose(V(-0.4f, -0.3f, 0.85f), V(0.6f, 0.78f, 0.15f), V(0.05f, -0.05f, 1f))));
        }

        // ---- Spear ------------------------------------------------------------------------------

        static WeaponClips Spear()
        {
            var stance = new Pose
            {
                hips = V(0f, -0.03f, 0f),
                hipsYaw = 12f,
                armR = new ArmPose(V(0.3f, -0.95f, 0.05f), V(0.05f, -0.85f, 0.5f), V(0.05f, 0.55f, 0.85f)),
                armL = new ArmPose(V(-0.25f, -0.95f, 0.2f), V(0.35f, -0.3f, 0.9f), V(0f, 1f, 0f)),
            };

            // Charged: overhead javelin grip, then a lunging thrust.
            var draw = stance.Copy();
            draw.hips = V(0f, -0.1f, -0.12f);
            draw.hipsYaw = 40f;
            draw.chestYaw = 25f;
            draw.chestPitch = -8f;
            draw.footR = V(0.18f, MannequinRig.AnkleHeight, -0.2f);
            draw.armR = new ArmPose(V(0.55f, 0.5f, -0.65f), V(0.05f, 0.95f, 0.25f), V(0f, -0.2f, 1f));
            draw.armL = new ArmPose(V(-0.3f, -0.2f, 0.95f), V(0.2f, 0.2f, 0.95f), V(0f, 1f, 0f));

            var tremble = draw.Copy();
            tremble.hips += V(0f, -0.01f, 0f);

            var lunge = stance.Copy();
            lunge.hips = V(0f, -0.15f, 0.3f);
            lunge.hipsYaw = -15f;
            lunge.chestYaw = -20f;
            lunge.chestPitch = 18f;
            lunge.footL = V(-0.12f, MannequinRig.AnkleHeight, 0.45f);
            lunge.armR = new ArmPose(V(0.15f, 0.05f, 1f), V(0.05f, 0.95f, -0.05f), V(0f, -0.1f, 1f));
            lunge.armL = new ArmPose(V(-0.35f, -0.7f, -0.4f), V(-0.1f, -0.9f, 0.3f), V(0f, 0f, 1f));

            var extend = lunge.Copy();
            extend.armR = new ArmPose(V(0.12f, -0.05f, 1f), V(0f, 0.9f, -0.1f), V(0f, -0.15f, 1f));

            var charged = new ClipSpec { name = "Spear_Charged" }
                .Key(0f, stance)
                .Key(0.32f, draw, Ease.Out)
                .Key(0.38f, tremble, Ease.Linear)
                .Key(0.50f, lunge, Ease.In)
                .Key(0.62f, extend, Ease.Out)
                .Key(0.95f, stance)
                .Event(0.04f, C.ChargeStartEvent)
                .Event(0.40f, C.StrikeOpenEvent)
                .Event(0.64f, C.StrikeCloseEvent);

            return Complete("Spear", stance, pace: 1f, charged,
                Guard("Spear_Guard", new ArmPose(V(-0.35f, -0.35f, 0.85f), V(0.9f, 0.35f, 0.2f), V(0f, 0.1f, 1f))));
        }

        // ---- Mace -------------------------------------------------------------------------------

        static WeaponClips Mace()
        {
            var stance = new Pose
            {
                hips = V(0f, -0.04f, 0f),
                hipsYaw = 8f,
                armR = new ArmPose(V(0.3f, -0.95f, 0.1f), V(0.05f, -0.7f, 0.7f), V(0.1f, 0.7f, 0.7f)),
                armL = new ArmPose(V(-0.3f, -0.9f, 0.2f), V(0.4f, -0.1f, 0.9f), V(0f, 1f, 0f)),
            };

            // Charged: wind far back behind the shoulder, then a crouching slam.
            var windup = stance.Copy();
            windup.hips = V(0f, -0.12f, -0.1f);
            windup.hipsYaw = 35f;
            windup.chestYaw = 30f;
            windup.chestPitch = -15f;
            windup.footR = V(0.17f, MannequinRig.AnkleHeight, -0.2f);
            windup.armR = new ArmPose(V(0.5f, 0.8f, -0.35f), V(0.2f, 0.6f, -0.8f), V(0.1f, -0.8f, -0.6f));

            var tremble = windup.Copy();
            tremble.hips += V(0f, -0.01f, 0f);

            var crush = stance.Copy();
            crush.hips = V(0f, -0.2f, 0.15f);
            crush.hipsYaw = -10f;
            crush.chestYaw = -20f;
            crush.chestPitch = 35f;
            crush.footL = V(-0.14f, MannequinRig.AnkleHeight, 0.38f);
            crush.armR = new ArmPose(V(0.1f, -0.35f, 0.95f), V(0f, -0.95f, -0.3f), V(0f, -0.3f, 0.95f));

            var deep = crush.Copy();
            deep.armR = new ArmPose(V(0.1f, -0.7f, 0.7f), V(0f, -0.6f, -0.8f), V(0f, -0.8f, 0.6f));

            var charged = new ClipSpec { name = "Mace_Charged" }
                .Key(0f, stance)
                .Key(0.32f, windup, Ease.Out)
                .Key(0.38f, tremble, Ease.Linear)
                .Key(0.52f, crush, Ease.In)
                .Key(0.66f, deep, Ease.Out)
                .Key(0.95f, stance)
                .Event(0.04f, C.ChargeStartEvent)
                .Event(0.40f, C.StrikeOpenEvent)
                .Event(0.66f, C.StrikeCloseEvent);

            // Heavier than the others: every shared motion plays 10 % slower.
            return Complete("Mace", stance, pace: 1.1f, charged,
                Guard("Mace_Guard", new ArmPose(V(-0.35f, -0.35f, 0.85f), V(0.9f, 0.35f, 0.2f), V(0f, 0.1f, 1f))));
        }
    }
}
