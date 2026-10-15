using CG4002.Combat;
using UnityEngine;
using C = CG4002.Models.AvatarContract;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Key poses for every weapon's clips. Directions are in avatar space:
    /// +X is the avatar's right, +Y up, +Z toward the opponent.
    ///
    /// Timing note: the classifier labels an action only after the player's motion window
    /// closes, so these clips arrive late by design. Wind-ups are kept short and strikes
    /// front-loaded so the avatar catches up with the real opponent quickly.
    /// </summary>
    static class AnimationLibrary
    {
        public class WeaponClips
        {
            public ClipSpec idle, light, charged, guard;
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

        // ---- Sword and shield -------------------------------------------------------------------

        static WeaponClips SwordAndShield()
        {
            var stance = new Pose
            {
                hips = V(0f, -0.03f, 0f),
                hipsYaw = 10f,
                chestYaw = 6f,
                chestPitch = 4f,
                armR = new ArmPose(V(0.28f, -0.95f, 0.15f), V(0.1f, -0.3f, 1f), V(0f, 1f, 0.55f)),
                armL = new ArmPose(V(-0.3f, -0.92f, 0.25f), V(0.5f, -0.6f, 0.6f), V(-0.1f, 0.2f, 1f)),
            };

            // Light: horizontal slash, right to left.
            var cock = stance.Copy();
            cock.hips = V(0f, -0.05f, -0.03f);
            cock.hipsYaw = 25f;
            cock.chestYaw = 30f;
            cock.armR = new ArmPose(V(0.9f, -0.1f, -0.3f), V(0.1f, -0.25f, 0.95f), V(0.55f, 0.35f, -0.75f));

            var cut = stance.Copy();
            cut.hips = V(0f, -0.07f, 0.05f);
            cut.hipsYaw = -10f;
            cut.chestYaw = -25f;
            cut.chestPitch = 8f;
            cut.footL = V(-0.14f, MannequinRig.AnkleHeight, 0.2f);
            cut.armR = new ArmPose(V(0.3f, -0.15f, 0.95f), V(-0.95f, -0.1f, 0.2f), V(0.1f, 0f, 1f));

            var follow = cut.Copy();
            follow.hipsYaw = -25f;
            follow.chestYaw = -40f;
            follow.armR = new ArmPose(V(-0.35f, -0.35f, 0.85f), V(-0.2f, -0.9f, -0.3f), V(-0.8f, -0.1f, 0.5f));

            var light = new ClipSpec { name = "Sword_Light" }
                .Key(0f, stance)
                .Key(0.14f, cock, Ease.Out)
                .Key(0.30f, cut, Ease.In)
                .Key(0.42f, follow, Ease.Out)
                .Key(0.62f, stance)
                .Event(0.16f, C.StrikeOpenEvent)
                .Event(0.44f, C.StrikeCloseEvent);

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

            return new WeaponClips
            {
                idle = Idle("Sword_Idle", stance),
                light = light,
                charged = charged,
                guard = Guard("Sword_Guard", new ArmPose(V(-0.4f, -0.3f, 0.85f), V(0.6f, 0.78f, 0.15f), V(0.05f, -0.05f, 1f))),
            };
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

            // Light: straight thrust.
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

            var light = new ClipSpec { name = "Spear_Light" }
                .Key(0f, stance)
                .Key(0.12f, chamber, Ease.Out)
                .Key(0.24f, thrust, Ease.In)
                .Key(0.34f, hold, Ease.Out)
                .Key(0.55f, stance)
                .Event(0.13f, C.StrikeOpenEvent)
                .Event(0.32f, C.StrikeCloseEvent);

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

            return new WeaponClips
            {
                idle = Idle("Spear_Idle", stance),
                light = light,
                charged = charged,
                guard = Guard("Spear_Guard", new ArmPose(V(-0.35f, -0.35f, 0.85f), V(0.9f, 0.35f, 0.2f), V(0f, 0.1f, 1f))),
            };
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

            // Light: vertical overhead swing.
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

            var light = new ClipSpec { name = "Mace_Light" }
                .Key(0f, stance)
                .Key(0.16f, raise, Ease.Out)
                .Key(0.30f, smash, Ease.In)
                .Key(0.42f, through, Ease.Out)
                .Key(0.60f, stance)
                .Event(0.17f, C.StrikeOpenEvent)
                .Event(0.42f, C.StrikeCloseEvent);

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

            return new WeaponClips
            {
                idle = Idle("Mace_Idle", stance),
                light = light,
                charged = charged,
                guard = Guard("Mace_Guard", new ArmPose(V(-0.35f, -0.35f, 0.85f), V(0.9f, 0.35f, 0.2f), V(0f, 0.1f, 1f))),
            };
        }
    }
}
