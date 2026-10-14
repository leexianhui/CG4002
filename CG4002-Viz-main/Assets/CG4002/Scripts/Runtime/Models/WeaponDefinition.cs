using System;
using CG4002.Combat;
using UnityEngine;

namespace CG4002.Models
{
    /// <summary>
    /// Everything that differs between weapons, as data: which models to attach,
    /// which animations to play, and how each classifier action maps to an animator slot.
    /// </summary>
    [CreateAssetMenu(menuName = "CG4002/Weapon Definition")]
    public class WeaponDefinition : ScriptableObject
    {
        [Serializable]
        public struct MoveBinding
        {
            public ActionType action;
            public AttackSlot slot;
        }

        public WeaponType type;
        public string displayName;

        [Header("Models")]
        public WeaponView mainHandPrefab;
        [Tooltip("Optional. Only the sword and shield uses an off-hand item.")]
        public ShieldView offHandPrefab;

        [Header("Animation")]
        [Tooltip("Supplies this weapon's clips for the shared avatar state machine.")]
        public AnimatorOverrideController animations;

        [Header("Effects")]
        [ColorUsage(false, true)]
        public Color accentColor = Color.cyan;

        [Header("Move set")]
        public MoveBinding[] moves = Array.Empty<MoveBinding>();

        public bool TryGetSlot(ActionType action, out AttackSlot slot)
        {
            foreach (var move in moves)
            {
                if (move.action == action)
                {
                    slot = move.slot;
                    return true;
                }
            }
            slot = default;
            return false;
        }
    }
}
