using CG4002.AR;
using CG4002.Combat;
using CG4002.Models;
using CG4002.State;
using UnityEngine;

namespace CG4002.Offline
{
    /// <summary>
    /// Offline only: stands in for the IMU and classifier. When the tracked blade touches
    /// the dummy, it sends an ActionMessage outbound; the engine side decides hit or block.
    /// The body is an upright capsule; each frame it checks the blade, and the path the tip
    /// took since last frame, so a fast swing that jumps past the dummy still counts.
    /// </summary>
    public class SwordHitDetector : MonoBehaviour
    {
        [SerializeField] PropTracker prop;
        [SerializeField] AvatarView dummy;
        [SerializeField] MessageDispatcher dispatcher;
        [SerializeField] GameState gameState;

        [Tooltip("Without the IMU there is no way to tell which move it was, so every contact counts as this.")]
        [SerializeField] ActionType contactAction = ActionType.HorizontalSwing;

        [Header("Dummy hit volume: an upright capsule, for a full-size (1.8 m) dummy, heights above its feet (metres)")]
        [SerializeField] float capsuleBottom = 0.3f;
        [SerializeField] float capsuleTop = 1.7f;
        [SerializeField] float capsuleRadius = 0.25f;

        bool wasTouching;
        bool hasPreviousTip;
        Vector3 previousTip;

        void Update()
        {
            // No sword on screen, or no dummy placed yet: nothing can be hit.
            if (!prop.IsSwordVisible || !dummy.gameObject.activeInHierarchy)
            {
                // Tracking lost: forget the last tip so a jump on re-acquire isn't read as a swing.
                wasTouching = false;
                hasPreviousTip = false;
                return;
            }

            Vector3 bladeBase = prop.Sword.trailBase.position;
            Vector3 tip = prop.Sword.trailTip.position;
            // Defined for a full-size dummy; scaled with it.
            Vector3 bottom = dummy.transform.TransformPoint(0f, capsuleBottom, 0f);
            Vector3 top = dummy.transform.TransformPoint(0f, capsuleTop, 0f);
            float radius = capsuleRadius * dummy.transform.lossyScale.x;

            bool touching =
                SegmentTouchesCapsule(bladeBase, tip, bottom, top, radius) ||
                (hasPreviousTip && SegmentTouchesCapsule(previousTip, tip, bottom, top, radius));

            // Only the moment of contact is an attack, not every frame the blade stays inside.
            if (touching && !wasTouching)
                dispatcher.Outbound.Enqueue(new ActionMessage { player = gameState.LocalPlayer, action = contactAction });

            wasTouching = touching;
            previousTip = tip;
            hasPreviousTip = true;
        }

        /// <summary>
        /// True if the segment a to b passes inside the capsule. Checks a point every
        /// half-radius, so even a long fast-swing path can't step over the body.
        /// </summary>
        public static bool SegmentTouchesCapsule(Vector3 a, Vector3 b, Vector3 capsuleBottom, Vector3 capsuleTop, float radius)
        {
            int samples = Mathf.Max(2, Mathf.CeilToInt(Vector3.Distance(a, b) / (radius * 0.5f)) + 1);
            for (int i = 0; i < samples; i++)
            {
                Vector3 point = Vector3.Lerp(a, b, i / (samples - 1f));
                Vector3 nearestOnAxis = ClosestPointOnSegment(point, capsuleBottom, capsuleTop);
                if (Vector3.Distance(point, nearestOnAxis) <= radius)
                    return true;
            }
            return false;
        }

        static Vector3 ClosestPointOnSegment(Vector3 point, Vector3 start, Vector3 end)
        {
            Vector3 segment = end - start;
            float t = Vector3.Dot(point - start, segment) / segment.sqrMagnitude;
            return start + segment * Mathf.Clamp01(t);
        }

        // Scene view: select the Game object to see the hit volume around the dummy.
        void OnDrawGizmosSelected()
        {
            if (dummy == null)
                return;
            Vector3 bottom = dummy.transform.TransformPoint(0f, capsuleBottom, 0f);
            Vector3 top = dummy.transform.TransformPoint(0f, capsuleTop, 0f);
            float radius = capsuleRadius * dummy.transform.lossyScale.x;
            Gizmos.color = Color.red;
            Gizmos.DrawWireSphere(bottom, radius);
            Gizmos.DrawWireSphere(top, radius);
            foreach (var side in new[] { Vector3.left, Vector3.right, Vector3.forward, Vector3.back })
            {
                Vector3 offset = dummy.transform.rotation * side * radius;
                Gizmos.DrawLine(bottom + offset, top + offset);
            }
        }
    }
}
