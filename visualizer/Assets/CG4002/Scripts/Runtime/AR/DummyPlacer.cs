using System.Collections.Generic;
using CG4002.State;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.InputSystem;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace CG4002.AR
{
    /// <summary>
    /// Tap-to-place: tapping a detected floor plane moves the arena (and the dummy standing at
    /// its origin) there, facing the camera, and asks the engine for a new match. Detected
    /// planes are drawn until then, and hidden after.
    /// </summary>
    public class DummyPlacer : MonoBehaviour
    {
        [SerializeField] ARRaycastManager raycastManager;
        [SerializeField] ARPlaneManager planeManager;
        [SerializeField] Camera arCamera;
        [SerializeField] MessageDispatcher dispatcher;
        [SerializeField] GameState gameState;

        [Tooltip("The arena: the dummy's parent. Placing moves this.")]
        [SerializeField] Transform arena;
        [SerializeField] Transform dummy;

        [Tooltip("How tall to draw the dummy, metres. The model is 1.8 m; smaller keeps it in view at sword's reach.")]
        [SerializeField] float dummyHeight = 1.44f;

        const float ModelHeight = 1.8f;

        readonly List<ARRaycastHit> hits = new();
        bool planesVisible;

        public bool IsPlaced { get; private set; }
        // An optional network/settings overlay may consume pointer input before AR placement.
        public System.Func<Vector2, bool> BlockPlacementInput;

        void OnEnable() => planeManager.trackablesChanged.AddListener(OnPlanesChanged);
        void OnDisable() => planeManager.trackablesChanged.RemoveListener(OnPlanesChanged);

        void Start()
        {
            dummy.localScale = Vector3.one * (dummyHeight / ModelHeight);
            BeginPlacement();
        }

        /// <summary>Hide the dummy and show the detected planes until the next tap on one.</summary>
        public void BeginPlacement()
        {
            IsPlaced = false;
            arena.gameObject.SetActive(false);
            SetPlanesVisible(true);
        }

        void Update()
        {
            if (IsPlaced)
                return;

            var pointer = Pointer.current;
            if (pointer == null || !pointer.press.wasPressedThisFrame)
                return;

            Vector2 screenPoint = pointer.position.ReadValue();
            if (BlockPlacementInput?.Invoke(screenPoint) == true) return;
            if (IsOverUi(screenPoint))
                return;

            // Only inside a detected plane's outline, not on an estimated surface.
            if (raycastManager.Raycast(screenPoint, hits, TrackableType.PlaneWithinPolygon))
                Place(hits[0].pose.position);
        }

        void Place(Vector3 floorPoint)
        {
            // Face the camera, turning only around the vertical so the dummy stands upright.
            Vector3 toCamera = arCamera.transform.position - floorPoint;
            toCamera.y = 0f;
            arena.SetPositionAndRotation(floorPoint, Quaternion.LookRotation(toCamera.normalized, Vector3.up));

            arena.gameObject.SetActive(true);
            SetPlanesVisible(false);
            IsPlaced = true;

            // The avatar's controller only listens while the arena is active, so it missed the
            // starting state; a new match sends it again.
            dispatcher.Outbound.Enqueue(new StartMatchRequestMessage { player = gameState.LocalPlayer });
        }

        // Deactivating a plane's GameObject hides it, but ARKit still tracks it and raycasts still
        // hit it. (Disabling just its renderers doesn't stick: AR Foundation re-enables them.)
        void SetPlanesVisible(bool visible)
        {
            planesVisible = visible;
            foreach (var plane in planeManager.trackables)
                plane.gameObject.SetActive(visible);
        }

        void OnPlanesChanged(ARTrackablesChangedEventArgs<ARPlane> args)
        {
            // Planes found after placing must start hidden too.
            foreach (var plane in args.added)
                plane.gameObject.SetActive(planesVisible);
        }

        /// <summary>True if the screen point is over a HUD button, so a button press isn't also a placement tap.</summary>
        static bool IsOverUi(Vector2 screenPoint)
        {
            if (EventSystem.current == null)
                return false;
            var results = new List<RaycastResult>();
            EventSystem.current.RaycastAll(new PointerEventData(EventSystem.current) { position = screenPoint }, results);
            return results.Count > 0;
        }
    }
}
