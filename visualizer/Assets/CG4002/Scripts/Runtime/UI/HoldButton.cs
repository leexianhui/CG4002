using UnityEngine;
using UnityEngine.EventSystems;

namespace CG4002.UI
{
    /// <summary>
    /// A button that reports being held, not clicked: down while a finger is on it, up when it lifts
    /// or slides off. Demo mode's GUARD uses it, so the guard is up exactly as long as it's held.
    /// Needs a raycast-target Graphic on the same object.
    /// </summary>
    public class HoldButton : MonoBehaviour, IPointerDownHandler, IPointerUpHandler, IPointerExitHandler
    {
        public bool Held { get; private set; }

        /// <summary>True when pressed, false when released.</summary>
        public event System.Action<bool> HeldChanged;

        public void OnPointerDown(PointerEventData eventData) => SetHeld(true);
        public void OnPointerUp(PointerEventData eventData) => SetHeld(false);
        public void OnPointerExit(PointerEventData eventData) => SetHeld(false);

        // Hidden mid-press (e.g. the match ended and the bar was hidden): the finger can't lift on it.
        void OnDisable() => SetHeld(false);

        void SetHeld(bool held)
        {
            if (Held == held)
                return;
            Held = held;
            HeldChanged?.Invoke(held);
        }
    }
}
