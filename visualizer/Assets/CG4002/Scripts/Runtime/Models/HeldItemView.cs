using UnityEngine;

namespace CG4002.Models
{
    /// <summary>Anything an avatar holds. The FX layer only needs to know what can glow.</summary>
    public abstract class HeldItemView : MonoBehaviour
    {
        [Tooltip("Renderers whose emission is ramped during a charged attack.")]
        public Renderer[] glowRenderers;
    }
}
