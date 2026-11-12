using CG4002.AR;
using UnityEngine;
using UnityEngine.XR.ARFoundation;

namespace CG4002.Net
{
    /// <summary>
    /// Common floor origin and +Z line, independent of prop tracking. The origin is where the arena
    /// was tapped; +Z points from it to where the player stands when confirming.
    ///
    /// Confirming happens by itself, so it can be done with the headset on: stand at least
    /// <see cref="minDistance"/> from the origin, look at the opponent avatar and hold still for
    /// <see cref="holdSeconds"/>. The online panel's button still confirms by hand.
    ///
    /// Short AR tracking dips (ARKit's Limited state: the camera facing a table or the floor, fast
    /// moves, low light) keep the calibration. Only a dip longer than <see cref="trackingGraceSeconds"/>
    /// reports not ready, and readiness comes back with tracking. Moving the arena or backgrounding
    /// the app clears it.
    /// </summary>
    public class SharedArenaCalibration : MonoBehaviour
    {
        public DummyPlacer placer;
        public Transform arena;
        public Camera arCamera;
        public bool requireARTracking = true;

        [Header("Auto-confirm")]
        public bool autoConfirm = true;
        [Tooltip("Seconds to look at the avatar and hold still before confirming.")]
        public float holdSeconds = 3f;
        [Tooltip("Metres from the origin, at least. Closer gives a poor direction.")]
        public float minDistance = 0.5f;
        [Tooltip("Degrees, on the floor, between where the camera looks and the avatar.")]
        public float facingTolerance = 20f;
        [Tooltip("How far the camera may drift during the hold (metres, degrees) before it starts again.")]
        public float stillDistance = 0.05f;
        public float stillDegrees = 6f;

        [Header("Tracking dips")]
        [Tooltip("Seconds without full AR tracking before reporting not ready (the engine then pauses).")]
        public float trackingGraceSeconds = 3f;

        Vector3 origin;
        Quaternion rotation;
        bool calibrated;
        float? trackingLostAt;
        float? holdStartedAt;
        Vector3 holdPosition;
        Quaternion holdRotation;

        public int Revision { get; private set; }

        /// <summary>Confirmed, though possibly not ready during a long tracking loss.</summary>
        public bool IsCalibrated
        {
            get { ValidateCalibration(); return calibrated; }
        }

        /// <summary>Confirmed and tracking (or within a short dip): positions can be trusted.</summary>
        public bool IsReady
        {
            get { ValidateCalibration(); return calibrated && !TrackingLost; }
        }

        /// <summary>Calibrated, but tracking has been gone longer than the grace period.</summary>
        public bool TrackingLost =>
            trackingLostAt.HasValue && Time.unscaledTime - trackingLostAt.Value >= trackingGraceSeconds;

        /// <summary>For the headset view while not calibrated: what to do next.</summary>
        public string Hint { get; private set; } = "";

        public string Status
        {
            get
            {
                if (!placer.IsPlaced) return "Tap the floor to place the arena";
                if (IsCalibrated) return TrackingLost ? "Tracking lost; look around slowly" : "Ready";
                if (!HasTracking) return "Waiting for AR tracking";
                return autoConfirm ? "Look at the avatar and hold still" : "Stand on +Z line and confirm direction";
            }
        }

        bool HasTracking => !requireARTracking || ARSession.state == ARSessionState.SessionTracking;
        bool OriginUnchanged => Vector3.Distance(origin, arena.position) <= 0.01f &&
            Quaternion.Angle(rotation, arena.rotation) <= 0.5f;

        void Update()
        {
            ValidateCalibration();
            if (autoConfirm && !calibrated)
                UpdateHold();
            else
                holdStartedAt = null;
        }

        void ValidateCalibration()
        {
            if (HasTracking)
                trackingLostAt = null;
            else
                trackingLostAt ??= Time.unscaledTime;
            // ARKit keeps its coordinates through tracking dips, so only a moved arena needs confirming again.
            if (!placer.IsPlaced || !OriginUnchanged) Invalidate();
        }

        public void Invalidate()
        {
            if (calibrated) Revision++;
            calibrated = false;
        }
        void OnApplicationPause(bool paused) { if (paused) Invalidate(); }

        /// <summary>Confirms once the camera has looked at the avatar, from far enough away, without moving for holdSeconds.</summary>
        void UpdateHold()
        {
            Transform view = arCamera.transform;
            if (!HasTracking)
                Hint = "Look around the room slowly";
            if (!HasTracking || !placer.IsPlaced || !LooksAtAvatar(view))
            {
                holdStartedAt = null;
                return;
            }
            if (!holdStartedAt.HasValue || Vector3.Distance(view.position, holdPosition) > stillDistance ||
                Quaternion.Angle(view.rotation, holdRotation) > stillDegrees)
            {
                holdStartedAt = Time.unscaledTime;
                holdPosition = view.position;
                holdRotation = view.rotation;
            }
            float remaining = holdSeconds - (Time.unscaledTime - holdStartedAt.Value);
            Hint = $"Hold still  {Mathf.Max(1, Mathf.CeilToInt(remaining))}";
            if (remaining <= 0f)
                ConfirmFromMarkedForwardLine();
        }

        bool LooksAtAvatar(Transform view)
        {
            Vector3 fromOrigin = Flat(view.position - arena.position);
            if (fromOrigin.magnitude < minDistance)
            {
                Hint = "Step back from your opponent";
                return false;
            }
            Transform avatar = placer.Dummy != null ? placer.Dummy : arena;
            Vector3 look = Flat(view.forward);
            if (look.sqrMagnitude < 0.04f || Vector3.Angle(look, Flat(avatar.position - view.position)) > facingTolerance)
            {
                Hint = "Look at your opponent and hold still";  // or looking almost straight down
                return false;
            }
            return true;
        }

        static Vector3 Flat(Vector3 v) => new(v.x, 0f, v.z);

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
            holdStartedAt = null;
            Revision++;
            return true;
        }
    }
}
