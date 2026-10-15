using CG4002.FX;
using UnityEngine;

namespace CG4002.AR
{
    /// <summary>
    /// A slash trail on the tracked sword. There is no animation event to say "the swing starts
    /// now" for a real prop, so the trail is driven by speed: it starts when the tip moves faster
    /// than startSpeed and stops when it slows below stopSpeed.
    /// </summary>
    public class SwordTrail : MonoBehaviour
    {
        [SerializeField] PropTracker prop;
        [SerializeField] Material trailMaterial;

        [ColorUsage(false, true)]
        [SerializeField] Color colour = new(0.45f, 1.3f, 1.5f);
        [Tooltip("Seconds each part of the trail stays visible.")]
        [SerializeField] float lifetime = 0.15f;

        [Tooltip("Tip speed (m/s) that starts the trail.")]
        [SerializeField] float startSpeed = 1.2f;
        [Tooltip("Tip speed (m/s) below which the trail stops. Lower than startSpeed, so a swing " +
                 "hovering around one speed doesn't flicker the trail on and off.")]
        [SerializeField] float stopSpeed = 0.6f;

        BladeTrail trail;
        Vector3 previousTip;
        bool hasPreviousTip;

        void Awake()
        {
            var trailObject = new GameObject("Tracked Sword Trail", typeof(MeshFilter), typeof(MeshRenderer), typeof(BladeTrail));
            trailObject.GetComponent<MeshRenderer>().sharedMaterial = trailMaterial;
            trail = trailObject.GetComponent<BladeTrail>();
        }

        void Update()
        {
            if (!prop.IsSwordVisible)
            {
                // Tracking lost: stop, and don't treat the jump on re-appearing as a swing.
                if (trail.IsEmitting)
                    trail.End();
                hasPreviousTip = false;
                return;
            }

            Vector3 tip = prop.Sword.trailTip.position;
            if (hasPreviousTip && Time.deltaTime > 0f)
            {
                float speed = Vector3.Distance(tip, previousTip) / Time.deltaTime;
                if (!trail.IsEmitting && speed > startSpeed)
                    trail.Begin(prop.Sword.trailBase, prop.Sword.trailTip, colour, lifetime);
                else if (trail.IsEmitting && speed < stopSpeed)
                    trail.End();
            }
            previousTip = tip;
            hasPreviousTip = true;
        }
    }
}
