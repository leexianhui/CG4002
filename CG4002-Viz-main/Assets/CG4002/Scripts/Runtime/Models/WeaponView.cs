using UnityEngine;

namespace CG4002.Models
{
    /// <summary>
    /// Contract for a main-hand weapon model. Pivot is the grip, +Y runs toward the tip.
    /// Controllers and effects use these anchors only, never the mesh hierarchy,
    /// so the geometry can be replaced without touching code.
    /// </summary>
    public class WeaponView : HeldItemView
    {
        [Tooltip("Point on the blade or haft nearest the hand. Inner edge of the slash trail.")]
        public Transform trailBase;

        [Tooltip("Tip of the blade or head. Outer edge of the slash trail.")]
        public Transform trailTip;

        [Tooltip("Where impact effects spawn when this weapon connects.")]
        public Transform strikePoint;

        // Scene view: select the weapon to see where its trail and impacts will be drawn.
        void OnDrawGizmosSelected()
        {
            Gizmos.color = Color.cyan;
            if (trailBase != null && trailTip != null)
                Gizmos.DrawLine(trailBase.position, trailTip.position);
            Gizmos.color = Color.red;
            if (strikePoint != null)
                Gizmos.DrawWireSphere(strikePoint.position, 0.03f);
        }
    }
}
