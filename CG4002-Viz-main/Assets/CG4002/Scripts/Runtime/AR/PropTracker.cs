using CG4002.Models;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace CG4002.AR
{
    /// <summary>
    /// Draws the sword model on the physical sword prop, using the marker ARKit tracks on the
    /// blade. ARKit gives the marker's pose; this works out where the grip and tip are from the
    /// prop's measurements, scales the model to the prop's length, and smooths the result.
    ///
    /// Marker frame (from AR Foundation): +Y out of the print, +Z towards the top of the picture,
    /// which is glued pointing up the blade. Weapon frame: pivot at the grip, +Y towards the tip.
    /// </summary>
    public class PropTracker : MonoBehaviour
    {
        [SerializeField] ARTrackedImageManager manager;
        [SerializeField] WeaponView swordPrefab;
        [Tooltip("Reference image names that count as the sword marker.")]
        [SerializeField] string[] swordImageNames = { "Sword", "SwordBackup" };

        [Header("Prop measurements (metres)")]
        [Tooltip("Along the blade, from the marker's centre down to the middle of the handle.")]
        [SerializeField] float markerToGripCentre = 0.155f;
        [Tooltip("Along the blade, from the marker's centre up to where the rendered tip should be. " +
                 "Can be longer than the prop. The model is scaled uniformly to fit, and looks its natural size " +
                 "when this plus Marker To Grip Centre is about 0.95.")]
        [SerializeField] float markerToTip = 0.8f;

        [Header("Tracking")]
        [Tooltip("Keep showing the sword this long after tracking drops, so brief dropouts don't flicker.")]
        [SerializeField] float holdSeconds = 0.3f;
        [Tooltip("How quickly the model follows the marker. Higher is snappier but shows more jitter.")]
        [SerializeField] float followSpeed = 25f;

        ARTrackedImage trackedImage;
        float lastTrackedTime = float.NegativeInfinity;

        /// <summary>The sword model, or null before the prop is first seen.</summary>
        public WeaponView Sword { get; private set; }

        /// <summary>True while the model is shown, i.e. tracked now or within holdSeconds.</summary>
        public bool IsSwordVisible => Sword != null && Sword.gameObject.activeSelf;

        void OnEnable() => manager.trackablesChanged.AddListener(OnTrackablesChanged);
        void OnDisable() => manager.trackablesChanged.RemoveListener(OnTrackablesChanged);

        void OnTrackablesChanged(ARTrackablesChangedEventArgs<ARTrackedImage> args)
        {
            foreach (var image in args.added)
            {
                if (trackedImage == null && IsSwordImage(image))
                    trackedImage = image;
            }
            foreach (var removed in args.removed)
            {
                if (removed.Value == trackedImage)
                    trackedImage = null;
            }
        }

        bool IsSwordImage(ARTrackedImage image) =>
            System.Array.IndexOf(swordImageNames, image.referenceImage.name) >= 0;

        void Update()
        {
            bool tracking = trackedImage != null && trackedImage.trackingState == TrackingState.Tracking;
            if (tracking)
            {
                if (Sword == null)
                    Sword = CreateSword();
                lastTrackedTime = Time.time;
                FollowMarker(snap: !Sword.gameObject.activeSelf);
            }
            if (Sword != null)
                Sword.gameObject.SetActive(Time.time - lastTrackedTime <= holdSeconds);
        }

        WeaponView CreateSword()
        {
            var sword = Instantiate(swordPrefab);
            sword.name = "Tracked Sword";
            // Start hidden: the first tracked frame then snaps it onto the prop instead of
            // gliding in from wherever the prefab was spawned.
            sword.gameObject.SetActive(false);
            // Scale the model so its tip lands on the prop's tip.
            float modelGripToTip = sword.trailTip.localPosition.y;
            sword.transform.localScale = Vector3.one * ((markerToGripCentre + markerToTip) / modelGripToTip);
            return sword;
        }

        void FollowMarker(bool snap)
        {
            Transform marker = trackedImage.transform;
            Vector3 upTheBlade = marker.forward;  // marker +Z, glued pointing at the tip
            Vector3 targetPosition = marker.position - upTheBlade * markerToGripCentre;
            // Weapon +Y runs up the blade; its flat faces the same way as the print.
            Quaternion targetRotation = Quaternion.LookRotation(marker.up, upTheBlade);

            if (snap)
            {
                Sword.transform.SetPositionAndRotation(targetPosition, targetRotation);
                return;
            }
            float t = 1f - Mathf.Exp(-followSpeed * Time.deltaTime);
            Sword.transform.SetPositionAndRotation(
                Vector3.Lerp(Sword.transform.position, targetPosition, t),
                Quaternion.Slerp(Sword.transform.rotation, targetRotation, t));
        }
    }
}
