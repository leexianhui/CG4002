using CG4002.AR;
using UnityEngine;
using UnityEngine.XR.ARFoundation;

namespace CG4002.Net
{
    /// <summary>Manual common floor origin and +Z line, independent of prop tracking.</summary>
    public class SharedArenaCalibration : MonoBehaviour
    {
        public DummyPlacer placer;
        public Transform arena;
        public Camera arCamera;
        public bool requireARTracking = true;
        Vector3 origin;
        Quaternion rotation;
        bool calibrated;
        public int Revision { get; private set; }

        bool HasTracking => !requireARTracking || ARSession.state == ARSessionState.SessionTracking;
        bool OriginUnchanged => Vector3.Distance(origin, arena.position) <= 0.01f &&
            Quaternion.Angle(rotation, arena.rotation) <= 0.5f;

        public bool IsReady
        {
            get { ValidateCalibration(); return calibrated; }
        }

        public string Status
        {
            get
            {
                if (!HasTracking) return "Waiting for AR tracking";
                if (!placer.IsPlaced) return "Tap the shared floor origin";
                return IsReady ? "Ready" : "Stand on +Z line and confirm direction";
            }
        }

        void Update() => ValidateCalibration();

        void ValidateCalibration()
        {
            // Tracking recovery can change AR coordinates. Require explicit reconfirmation.
            if (!HasTracking || !placer.IsPlaced || !OriginUnchanged) Invalidate();
        }

        public void Invalidate()
        {
            if (calibrated) Revision++;
            calibrated = false;
        }
        void OnApplicationPause(bool paused) { if (paused) Invalidate(); }

        /// <summary>Stand on the same marked +Z line on both phones, after tapping the common origin.</summary>
        public bool ConfirmFromMarkedForwardLine()
        {
            if (!HasTracking || !placer.IsPlaced) return false;
            Vector3 forward = arCamera.transform.position - arena.position;
            forward.y = 0;
            if (forward.magnitude < 0.3f) return false;
            arena.rotation = Quaternion.LookRotation(forward.normalized, Vector3.up);
            origin = arena.position;
            rotation = arena.rotation;
            calibrated = true;
            Revision++;
            return true;
        }
    }
}
