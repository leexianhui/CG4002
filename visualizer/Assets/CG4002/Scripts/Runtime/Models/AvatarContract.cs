using System;
using CG4002.Combat;

namespace CG4002.Models
{
    /// <summary>
    /// Names shared between the generated animation assets and the code that drives them.
    /// Renaming anything here means regenerating the assets (CG4002 > Generate Models).
    /// </summary>
    public static class AvatarContract
    {
        // Animator parameters
        public const string AttackTrigger = "Attack";
        public const string AttackSlotInt = "AttackSlot";

        // Animator layers
        public const int BodyLayer = 0;
        public const int GuardLayer = 1;

        // States in the base controller. Each weapon overrides the clip behind each one.
        public const string IdleState = "Idle";
        public const string GuardState = "Guard";

        /// <summary>Every AttackSlot has a body-layer state of the same name.</summary>
        public static string AttackState(AttackSlot slot) => slot.ToString();

        public static readonly AttackSlot[] AttackSlots = (AttackSlot[])Enum.GetValues(typeof(AttackSlot));

        // Animation Event function names, received by AvatarView
        public const string ChargeStartEvent = "ChargeStart";
        public const string StrikeOpenEvent = "StrikeOpen";
        public const string StrikeCloseEvent = "StrikeClose";
    }
}
