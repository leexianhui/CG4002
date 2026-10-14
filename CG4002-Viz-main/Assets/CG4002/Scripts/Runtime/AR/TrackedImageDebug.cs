using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace CG4002.AR
{
    /// <summary>
    /// Tracking overlay: outlines every tracked image and lists its state and distance on
    /// screen. Toggled by the TRACKING button.
    ///
    /// AR Foundation places each ARTrackedImage at the image centre with the image in its
    /// local XZ plane: +Y is the surface normal (towards the camera when you face the print)
    /// and +Z points to the top edge of the picture.
    /// </summary>
    public class TrackedImageDebug : MonoBehaviour
    {
        [SerializeField] ARTrackedImageManager manager;
        [SerializeField] Camera arCamera;
        [Tooltip("Unlit material for the outlines. An asset reference, so the shader isn't stripped from builds.")]
        [SerializeField] Material material;

        class Entry
        {
            public ARTrackedImage image;
            public GameObject visual;
            public Renderer[] renderers;
            public float lastTrackedTime;
        }

        readonly Dictionary<TrackableId, Entry> entries = new();
        GUIStyle style;

        /// <summary>Show the outlines and readout. Tracking itself carries on either way.</summary>
        public bool Visible { get; set; } = true;

        void OnEnable() => manager.trackablesChanged.AddListener(OnChanged);
        void OnDisable() => manager.trackablesChanged.RemoveListener(OnChanged);

        void OnChanged(ARTrackablesChangedEventArgs<ARTrackedImage> args)
        {
            foreach (var image in args.added)
                entries[image.trackableId] = CreateEntry(image);
            foreach (var pair in args.removed)
            {
                if (entries.Remove(pair.Key, out var entry) && entry.visual != null)
                    Destroy(entry.visual);
            }
        }

        Entry CreateEntry(ARTrackedImage image)
        {
            Vector2 size = image.size;
            var root = new GameObject($"Debug {image.referenceImage.name}");
            root.transform.SetParent(image.transform, false);

            var parts = new List<Renderer>();
            float t = 0.004f;  // outline thickness, metres
            // Outline of the image rectangle, so size errors are obvious.
            parts.Add(Bar(root, new Vector3(0, 0, size.y / 2), new Vector3(size.x, t, t)));
            parts.Add(Bar(root, new Vector3(0, 0, -size.y / 2), new Vector3(size.x, t, t)));
            parts.Add(Bar(root, new Vector3(size.x / 2, 0, 0), new Vector3(t, t, size.y)));
            parts.Add(Bar(root, new Vector3(-size.x / 2, 0, 0), new Vector3(t, t, size.y)));
            // Arrow towards the top of the picture, and a short stalk out of the surface.
            parts.Add(Bar(root, new Vector3(0, 0, size.y * 0.25f), new Vector3(t * 2, t, size.y * 0.5f)));
            parts.Add(Bar(root, new Vector3(0, 0.03f, 0), new Vector3(t * 2, 0.06f, t * 2)));

            return new Entry { image = image, visual = root, renderers = parts.ToArray() };
        }

        Renderer Bar(GameObject parent, Vector3 position, Vector3 scale)
        {
            var bar = GameObject.CreatePrimitive(PrimitiveType.Cube);
            Destroy(bar.GetComponent<Collider>());
            bar.transform.SetParent(parent.transform, false);
            bar.transform.localPosition = position;
            bar.transform.localScale = scale;
            var r = bar.GetComponent<Renderer>();
            r.sharedMaterial = material;
            return r;
        }

        void Update()
        {
            foreach (var entry in entries.Values)
            {
                bool tracking = entry.image.trackingState == TrackingState.Tracking;
                if (tracking)
                    entry.lastTrackedTime = Time.time;
                // Green = tracking, yellow = limited (pose not updating), hidden = lost.
                var color = tracking ? Color.green : Color.yellow;
                foreach (var r in entry.renderers)
                    r.material.color = color;
                entry.visual.SetActive(Visible && entry.image.trackingState != TrackingState.None);
            }
        }

        void OnGUI()
        {
            if (!Visible)
                return;
            style ??= new GUIStyle(GUI.skin.box) { fontSize = 34, alignment = TextAnchor.UpperLeft, richText = true };
            var lines = new List<string> { "<b>Image tracking</b>" };
            if (entries.Count == 0)
                lines.Add("no images detected yet");
            foreach (var entry in entries.Values)
            {
                var image = entry.image;
                float distance = arCamera != null ? Vector3.Distance(arCamera.transform.position, image.transform.position) : 0f;
                string state = image.trackingState switch
                {
                    TrackingState.Tracking => "<color=#7CFC00>TRACKING</color>",
                    TrackingState.Limited => "<color=yellow>LIMITED</color>",
                    _ => "<color=red>NONE</color>",
                };
                float lost = Time.time - entry.lastTrackedTime;
                lines.Add($"{image.referenceImage.name}: {state}  {distance:0.00} m" +
                          (image.trackingState == TrackingState.Tracking ? "" : $"  (lost {lost:0.0}s)"));
            }
            // Bottom-left, clear of the HUD along the top of the screen.
            float height = 60 + 50 * lines.Count;
            GUI.Box(new Rect(30, Screen.height - height - 30, 900, height), string.Join("\n", lines), style);
        }
    }
}
