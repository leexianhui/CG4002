using UnityEngine;

namespace CG4002.Models
{
    /// <summary>Contract for an off-hand shield. Pivot is the arm strap, +Z is the face.</summary>
    public class ShieldView : HeldItemView
    {
        [Tooltip("Centre of the shield face. Block and parry effects spawn here.")]
        public Transform blockFace;

        void OnDrawGizmosSelected()
        {
            if (blockFace == null)
                return;
            Gizmos.color = Color.yellow;
            Gizmos.DrawRay(blockFace.position, blockFace.forward * 0.2f);
        }
    }
}
