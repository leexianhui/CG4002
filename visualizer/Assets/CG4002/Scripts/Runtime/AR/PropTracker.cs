using System;
using System.Collections.Generic;
using CG4002.Combat;
using CG4002.Models;
using CG4002.State;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace CG4002.AR
{
    /// <summary>
    /// Draws the held weapon's model on its physical prop, using the marker ARKit tracks on the
    /// prop. ARKit gives the marker's pose; this works out where the grip and tip are from the
    /// prop's measurements, scales the model to the prop's length, and smooths the result.
    ///
    /// Each weapon's prop has its own marker and measurements (Props). The marker decides: whichever
    /// prop's marker is seen gets its own weapon drawn, whatever weapon is selected. If several are
    /// seen, the one already shown stays, then the selected weapon (GameState) is preferred.
    ///
    /// Marker frame (from AR Foundation): +Y out of the print, +Z towards the top of the picture,
    /// which is glued pointing toward the weapon's tip. Weapon frame: pivot at the weapon hand's
    /// grip, +Y towards the tip.
    /// </summary>
    public class PropTracker : MonoBehaviour
    {
        [Serializable]
        public class Prop
        {
            public WeaponType weapon;
            [Tooltip("Reference image names that count as this prop's marker.")]
            public string[] imageNames;
            public WeaponView prefab;

            [Header("Measured on the prop (metres)")]
            [Tooltip("Along the weapon, from the marker's centre back to the middle of the weapon hand's grip.")]
            public float markerToGripCentre;
            [Tooltip("Along the weapon, from the marker's centre up to where the rendered tip should be. Can be " +
                     "longer than the prop. The model is scaled uniformly to fit, and looks its natural size when " +
                     "this plus Marker To Grip Centre equals the model's grip-to-tip length.")]
            public float markerToTip;
        }

        [SerializeField] ARTrackedImageManager manager;
        [Tooltip("Optional. When several props' markers are seen at once, prefer the weapon this player holds.")]
        [SerializeField] GameState gameState;
        [SerializeField] Prop[] props = Array.Empty<Prop>();

        [Header("Tracking")]
        [Tooltip("Keep showing the weapon this long after tracking drops, so brief dropouts don't flicker.")]
        [SerializeField] float holdSeconds = 0.3f;
        [Tooltip("How quickly the model follows the marker. Higher is snappier but shows more jitter.")]
        [SerializeField] float followSpeed = 25f;

        readonly Dictionary<string, ARTrackedImage> images = new();
        readonly Dictionary<WeaponType, WeaponView> models = new();
        Prop current;
        float lastTrackedTime = float.NegativeInfinity;

        /// <summary>The model on the prop being tracked, or null before any prop is seen.</summary>
        public WeaponView Weapon { get; private set; }

        /// <summary>
        /// Hides the weapon model without stopping tracking, e.g. while the first-person attack
        /// animation shows the weapon instead. It reappears at the tracked pose when cleared.
        /// </summary>
        public bool Suppressed { get; set; }

        /// <summary>True while the model is shown, i.e. tracked now or within holdSeconds.</summary>
        public bool IsWeaponVisible => Weapon != null && Weapon.gameObject.activeSelf;

        void OnEnable() => manager.trackablesChanged.AddListener(OnTrackablesChanged);
        void OnDisable() => manager.trackablesChanged.RemoveListener(OnTrackablesChanged);

        void OnTrackablesChanged(ARTrackablesChangedEventArgs<ARTrackedImage> args)
        {
            foreach (var image in args.added)
                images[image.referenceImage.name] = image;
            foreach (var removed in args.removed)
            {
                foreach (var (name, image) in new List<KeyValuePair<string, ARTrackedImage>>(images))
                {
                    if (image == removed.Value)
                        images.Remove(name);
                }
            }
        }

        void Update()
        {
            var seen = SeenProp();
            if (seen != null && seen != current)
            {
                if (Weapon != null)
                    Weapon.gameObject.SetActive(false);
                current = seen;
                Weapon = ModelFor(current);
                lastTrackedTime = float.NegativeInfinity;
            }
            if (current == null)
                return;

            var marker = TrackedMarker(current);
            if (marker != null)
            {
                lastTrackedTime = Time.time;
                FollowMarker(marker, snap: !Weapon.gameObject.activeSelf);
            }
            Weapon.gameObject.SetActive(!Suppressed && Time.time - lastTrackedTime <= holdSeconds);
        }

        /// <summary>The prop whose marker is tracked now: the one already shown, else the selected weapon's, else any.</summary>
        Prop SeenProp()
        {
            Prop selected = null, any = null;
            foreach (var prop in props)
            {
                if (TrackedMarker(prop) == null)
                    continue;
                if (prop == current)
                    return prop;
                if (gameState != null && prop.weapon == gameState.Self.weapon)
                    selected = prop;
                any ??= prop;
            }
            return selected ?? any;
        }

        ARTrackedImage TrackedMarker(Prop prop)
        {
            foreach (var name in prop.imageNames)
            {
                if (images.TryGetValue(name, out var image) && image.trackingState == TrackingState.Tracking)
                    return image;
            }
            return null;
        }

        WeaponView ModelFor(Prop prop)
        {
            if (models.TryGetValue(prop.weapon, out var existing))
                return existing;

            var model = Instantiate(prop.prefab);
            model.name = $"Tracked {prop.weapon}";
            // Start hidden: the first tracked frame then snaps it onto the prop instead of
            // gliding in from wherever the prefab was spawned.
            model.gameObject.SetActive(false);
            // Scale the model so its tip lands on the prop's tip.
            float modelGripToTip = model.trailTip.localPosition.y;
            model.transform.localScale = Vector3.one * ((prop.markerToGripCentre + prop.markerToTip) / modelGripToTip);
            models[prop.weapon] = model;
            return model;
        }

        void FollowMarker(ARTrackedImage marker, bool snap)
        {
            Vector3 upTheWeapon = marker.transform.forward;  // marker +Z, glued pointing at the tip
            Vector3 targetPosition = marker.transform.position - upTheWeapon * current.markerToGripCentre;
            // Weapon +Y runs up the weapon; its flat faces the same way as the print.
            Quaternion targetRotation = Quaternion.LookRotation(marker.transform.up, upTheWeapon);

            if (snap)
            {
                Weapon.transform.SetPositionAndRotation(targetPosition, targetRotation);
                return;
            }
            float t = 1f - Mathf.Exp(-followSpeed * Time.deltaTime);
            Weapon.transform.SetPositionAndRotation(
                Vector3.Lerp(Weapon.transform.position, targetPosition, t),
                Quaternion.Slerp(Weapon.transform.rotation, targetRotation, t));
        }
    }
}
