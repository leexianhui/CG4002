using System.Collections.Generic;
using UnityEngine;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Skeleton of the opponent mannequin. Bone paths are what animation clips bind to,
    /// so the model builder and the animation baker both read them from here.
    /// Units are metres, root at the feet, +Z forward, +X to the avatar's right.
    /// Limb bones point down (-Y) in the rest pose.
    /// </summary>
    static class MannequinRig
    {
        public const string Hips = "Hips";
        public const string Spine = Hips + "/Spine";
        public const string Chest = Spine + "/Chest";
        public const string Neck = Chest + "/Neck";
        public const string Head = Neck + "/Head";

        public const string UpperArmR = Chest + "/UpperArm_R";
        public const string ForearmR = UpperArmR + "/Forearm_R";
        public const string HandR = ForearmR + "/Hand_R";
        public const string UpperArmL = Chest + "/UpperArm_L";
        public const string ForearmL = UpperArmL + "/Forearm_L";
        public const string HandL = ForearmL + "/Hand_L";

        public const string UpperLegR = Hips + "/UpperLeg_R";
        public const string LowerLegR = UpperLegR + "/LowerLeg_R";
        public const string FootR = LowerLegR + "/Foot_R";
        public const string UpperLegL = Hips + "/UpperLeg_L";
        public const string LowerLegL = UpperLegL + "/LowerLeg_L";
        public const string FootL = LowerLegL + "/Foot_L";

        public static readonly Vector3 HipsRest = new(0f, 0.93f, 0f);
        public const float ShoulderWidth = 0.22f;
        public const float HipWidth = 0.1f;
        public const float UpperArmLength = 0.28f;
        public const float ForearmLength = 0.25f;
        public const float ThighLength = 0.44f;
        public const float ShinLength = 0.42f;
        public const float AnkleHeight = 0.03f;

        /// <summary>Hip joint offset from the Hips bone, before hip rotation.</summary>
        public static Vector3 HipJoint(float side) => new(HipWidth * side, -0.04f, 0f);

        /// <summary>Bones parent-first with their rest local positions.</summary>
        public static IEnumerable<(string path, Vector3 localPosition)> Bones()
        {
            yield return (Hips, HipsRest);
            yield return (Spine, new Vector3(0f, 0.10f, 0f));
            yield return (Chest, new Vector3(0f, 0.18f, 0f));
            yield return (Neck, new Vector3(0f, 0.30f, 0f));
            yield return (Head, new Vector3(0f, 0.07f, 0f));

            foreach (float side in new[] { 1f, -1f })
            {
                string s = side > 0 ? "_R" : "_L";
                string upperArm = $"{Chest}/UpperArm{s}";
                string forearm = $"{upperArm}/Forearm{s}";
                yield return (upperArm, new Vector3(ShoulderWidth * side, 0.25f, 0f));
                yield return (forearm, new Vector3(0f, -UpperArmLength, 0f));
                yield return ($"{forearm}/Hand{s}", new Vector3(0f, -ForearmLength, 0f));

                string upperLeg = $"{Hips}/UpperLeg{s}";
                string lowerLeg = $"{upperLeg}/LowerLeg{s}";
                yield return (upperLeg, HipJoint(side));
                yield return (lowerLeg, new Vector3(0f, -ThighLength, 0f));
                yield return ($"{lowerLeg}/Foot{s}", new Vector3(0f, -ShinLength, 0f));
            }
        }

        /// <summary>Bones that carry animation curves in full-body clips.</summary>
        public static readonly string[] AnimatedBones =
        {
            Hips, Spine, Chest, Head,
            UpperArmR, ForearmR, UpperArmL, ForearmL,
            UpperLegR, LowerLegR, FootR, UpperLegL, LowerLegL, FootL,
        };

        /// <summary>Bones the off-hand guard layer is allowed to drive.</summary>
        public static readonly string[] GuardBones = { UpperArmL, ForearmL };
    }
}
