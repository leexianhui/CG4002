using System;
using CG4002.Net;
using CG4002.Offline;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.UI;

namespace CG4002.AR
{
    /// <summary>
    /// Split screen for the headset: the same view twice, side by side, one per lens. Setup happens
    /// with the phone in hand at full view (connect, weapon pose, tap the floor, calibrate); once the
    /// arena is placed (and, online, calibrated) the view splits. It goes back to full view when the
    /// arena is picked up again (MOVE DUMMY, weapon selection), and never splits in demo mode, which
    /// needs the touchscreen. A double tap anywhere toggles it by hand (until the next placement).
    ///
    /// How: the AR camera renders into an eye-sized texture instead of the screen, with the HUD and
    /// first-person canvases switched to render into it too (Screen Space - Camera), so both eyes get
    /// the whole picture. An overlay then shows that texture twice on black, side by side and touching
    /// in the middle, with the black border at the outer edges. Mono: the phone has one camera, so both eyes see the same
    /// image. BorderedView still sets the camera picture's crop and projection for the eye's shape.
    /// </summary>
    [RequireComponent(typeof(Camera))]
    public class SplitScreen : MonoBehaviour
    {
        [Tooltip("One eye's image, as a fraction of the screen's width and height. Two of them side by side: 0.4 wide is 80% in all.")]
        [SerializeField] Vector2 eyeSize = new(0.4f, 0.9f);
        [SerializeField] DummyPlacer placer;
        [Tooltip("Optional (OnlineScene). While it's switched on, the view only splits once calibrated.")]
        [SerializeField] SharedArenaCalibration calibration;
        [Tooltip("Optional. Never split in demo mode.")]
        [SerializeField] DemoMode demo;
        [Tooltip("Screen-space canvases to show in both eyes (HUD, first-person, weapon selection).")]
        [SerializeField] Canvas[] canvases = Array.Empty<Canvas>();
        [SerializeField] float doubleTapSeconds = 0.35f;

        /// <summary>Whether the view is split now (SplitLayout switches the HUD to its split layout).</summary>
        public static bool Active { get; private set; }
        public static event Action<bool> Changed;

        Camera arCamera;
        RenderTexture eye;
        GameObject output;
        RawImage leftEye, rightEye;
        Camera screenClear;
        bool dismissed;
        bool wasPlaced;
        float lastTap = float.NegativeInfinity;

        bool Wanted
        {
            get
            {
                if (dismissed || placer == null || !placer.IsPlaced)
                    return false;
                if (demo != null && demo.Active)
                    return false;
                return calibration == null || !calibration.isActiveAndEnabled || calibration.IsReady;
            }
        }

        void Awake()
        {
            arCamera = GetComponent<Camera>();
            BuildOutput();
        }

        void OnDisable() => SetActive(false);

        void OnDestroy()
        {
            if (eye != null)
                eye.Release();
            if (output != null)
                Destroy(output);
            if (screenClear != null)
                Destroy(screenClear.gameObject);
        }

        void Update()
        {
            bool placed = placer != null && placer.IsPlaced;
            if (placed && !wasPlaced)
                dismissed = false;  // each placement splits again
            wasPlaced = placed;

            var pointer = Pointer.current;
            if (placed && pointer != null && pointer.press.wasPressedThisFrame)
            {
                if (Time.unscaledTime - lastTap <= doubleTapSeconds)
                {
                    dismissed = Active;  // split -> full view; full view (dismissed) -> split
                    lastTap = float.NegativeInfinity;
                }
                else
                    lastTap = Time.unscaledTime;
            }

            SetActive(Wanted);
            if (Active)
                FitEyeTexture();
        }

        void SetActive(bool active)
        {
            if (Active == active || arCamera == null)
                return;
            Active = active;
            if (active)
            {
                FitEyeTexture();
                arCamera.rect = new Rect(0f, 0f, 1f, 1f);  // BorderedView keeps it so while split
            }
            else
                arCamera.targetTexture = null;
            output.SetActive(active);
            screenClear.gameObject.SetActive(active);

            foreach (var canvas in canvases)
            {
                if (canvas == null)
                    continue;
                if (active)
                {
                    canvas.renderMode = RenderMode.ScreenSpaceCamera;
                    canvas.worldCamera = arCamera;
                    // Just past the near plane, in front of everything else (the first-person arm is 0.3 m out).
                    canvas.planeDistance = arCamera.nearClipPlane + 0.02f;
                }
                else
                    canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            }
            Changed?.Invoke(active);
        }

        /// <summary>The eye texture matches the screen (new on rotation or resize).</summary>
        void FitEyeTexture()
        {
            int width = Mathf.Max(16, Mathf.RoundToInt(Screen.width * eyeSize.x));
            int height = Mathf.Max(16, Mathf.RoundToInt(Screen.height * eyeSize.y));
            if (eye == null || eye.width != width || eye.height != height)
            {
                arCamera.targetTexture = null;
                if (eye != null)
                    eye.Release();
                eye = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32, RenderTextureReadWrite.Default)
                {
                    name = "Split Screen Eye",
                };
                leftEye.texture = rightEye.texture = eye;
            }
            arCamera.targetTexture = eye;
        }

        /// <summary>
        /// The overlay that shows the eye texture twice on black, and a camera that clears the screen
        /// behind it (no other camera draws to the screen while split).
        /// </summary>
        void BuildOutput()
        {
            output = new GameObject("Split Screen Output", typeof(Canvas));
            var canvas = output.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 1000;

            var black = NewImage<Image>(output.transform, "Black", Vector2.zero, Vector2.one);
            black.color = Color.black;
            leftEye = NewImage<RawImage>(output.transform, "Left Eye", EyeMin(0.5f - eyeSize.x / 2f), EyeMax(0.5f - eyeSize.x / 2f));
            rightEye = NewImage<RawImage>(output.transform, "Right Eye", EyeMin(0.5f + eyeSize.x / 2f), EyeMax(0.5f + eyeSize.x / 2f));
            output.SetActive(false);

            screenClear = new GameObject("Split Screen Clear").AddComponent<Camera>();
            screenClear.clearFlags = CameraClearFlags.SolidColor;
            screenClear.backgroundColor = Color.black;
            screenClear.cullingMask = 0;
            screenClear.depth = arCamera.depth + 1f;
            screenClear.gameObject.SetActive(false);
        }

        // The two eyes meet at the middle of the screen, with no gap; the border is at the outer edges.
        Vector2 EyeMin(float centre) => new(centre - eyeSize.x / 2f, 0.5f - eyeSize.y / 2f);
        Vector2 EyeMax(float centre) => new(centre + eyeSize.x / 2f, 0.5f + eyeSize.y / 2f);

        static T NewImage<T>(Transform parent, string name, Vector2 anchorMin, Vector2 anchorMax) where T : Graphic
        {
            var rect = new GameObject(name, typeof(RectTransform)).GetComponent<RectTransform>();
            rect.SetParent(parent, false);
            rect.anchorMin = anchorMin;
            rect.anchorMax = anchorMax;
            rect.offsetMin = rect.offsetMax = Vector2.zero;
            var image = rect.gameObject.AddComponent<T>();
            image.raycastTarget = false;
            return image;
        }
    }
}
