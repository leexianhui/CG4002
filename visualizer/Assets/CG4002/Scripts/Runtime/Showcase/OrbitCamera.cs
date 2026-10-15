using UnityEngine;
using UnityEngine.InputSystem;

namespace CG4002.Showcase
{
    /// <summary>Right-drag to orbit, scroll to zoom. Showcase scene only; the AR camera is driven by tracking.</summary>
    public class OrbitCamera : MonoBehaviour
    {
        public Vector3 target = new(0f, 1.0f, 0f);
        public float distance = 3.6f;
        public float yaw = 205f;
        public float pitch = 8f;
        [SerializeField] float orbitSpeed = 0.25f;
        [SerializeField] float zoomSpeed = 0.002f;

        void LateUpdate()
        {
            var mouse = Mouse.current;
            if (mouse != null)
            {
                if (mouse.rightButton.isPressed)
                {
                    Vector2 delta = mouse.delta.ReadValue();
                    yaw += delta.x * orbitSpeed;
                    pitch = Mathf.Clamp(pitch - delta.y * orbitSpeed, -10f, 70f);
                }
                distance = Mathf.Clamp(distance - mouse.scroll.ReadValue().y * zoomSpeed, 1.2f, 10f);
            }
            Apply();
        }

        public void Apply()
        {
            Quaternion rotation = Quaternion.Euler(pitch, yaw, 0f);
            transform.SetPositionAndRotation(target - rotation * Vector3.forward * distance, rotation);
        }
    }
}
