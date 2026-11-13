using System;
using CG4002.Offline;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.UI;
using UnityEngine.XR.ARFoundation;

namespace CG4002.AR
{
    /// <summary>
    /// Split screen for the headset: the same view twice, side by side, one per lens. Setup happens
    /// with the phone in hand at full view (connect, tap the floor); once the arena is placed the view
    /// splits, and the headset goes on for the rest (calibration by looking at the opponent, weapon
    /// selection, the match). It goes back to full view when the arena is picked up again (MOVE DUMMY),
    /// and never splits in demo mode, which needs the touchscreen. A double tap anywhere toggles it by
    /// hand (until the next placement).
    ///
    /// How: the AR camera renders into an eye-sized texture instead of the screen, with the HUD and
    /// first-person canvases switched to render into it too (Screen Space - Camera), so both eyes get
    /// the whole picture. An overlay then shows that texture twice on black, side by side and touching
    /// in the middle, with the black border at the outer edges. Mono: the phone has one camera, so both eyes see the same
    /// image. BorderedView still sets the camera picture's crop and projection for the eye's shape.
    ///
    /// Whole picture: each eye is nearly square, but the camera's picture is 4:3, so
    /// filling the eye would crop the picture's sides (about 47 degrees across instead of the
    /// camera's ~63). Instead the eye texture takes the picture's own shape and sits centred in the
    /// eye's box, with thin black strips above and below: the whole camera width, nothing cropped.
    /// The HUD is laid out inside the picture.
    /// </summary>
    [RequireComponent(typeof(Camera))]
    public class SplitScreen : MonoBehaviour
    {
        [Tooltip("One eye's image, as a fraction of the screen's width and height. Two of them side by side: 0.4 wide is 80% in all.")]
        [SerializeField] Vector2 eyeSize = new(0.4f, 0.9f);
        [SerializeField] DummyPlacer placer;
        [Tooltip("Optional. Never split in demo mode.")]
        [SerializeField] DemoMode demo;
        [Tooltip("Screen-space canvases to show in both eyes (HUD, first-person, weapon selection).")]
        [SerializeField] Canvas[] canvases = Array.Empty<Canvas>();
        [SerializeField] float doubleTapSeconds = 0.35f;

        /// <summary>Whether the view is split now (SplitLayout switches the HUD to its split layout).</summary>
        public static bool Active { get; private set; }
        public static event Action<bool> Changed;

        Camera arCamera;
        ARCameraManager cameraManager;
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
                return demo == null || !demo.Active;
            }
        }

        void Awake()
        {
            arCamera = GetComponent<Camera>();
            cameraManager = GetComponent<ARCameraManager>();
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

        /// <summary>
        /// The eye texture: the largest rectangle of the camera picture's shape inside the eye's box,
        /// centred in it (the whole box until the camera reports its resolution). New on rotation or resize.
        /// </summary>
        void FitEyeTexture()
        {
            float boxWidth = Screen.width * eyeSize.x, boxHeight = Screen.height * eyeSize.y;
            float shown = 1f;  // the share of the box's height the picture fills
            float width = boxWidth, height = boxHeight;
            if (PictureAspect() is float aspect)
            {
                if (boxWidth / boxHeight < aspect)
                    height = boxWidth / aspect;
                else
                    width = boxHeight * aspect;
                shown = height / boxHeight;
            }
            PlaceEye(leftEye, 0.5f - eyeSize.x / 2f, width / boxWidth, shown);
            PlaceEye(rightEye, 0.5f + eyeSize.x / 2f, width / boxWidth, shown);

            int pixelsWide = Mathf.Max(16, Mathf.RoundToInt(width));
            int pixelsHigh = Mathf.Max(16, Mathf.RoundToInt(height));
            if (eye == null || eye.width != pixelsWide || eye.height != pixelsHigh)
            {
                arCamera.targetTexture = null;
                if (eye != null)
                    eye.Release();
                eye = new RenderTexture(pixelsWide, pixelsHigh, 24, RenderTextureFormat.ARGB32, RenderTextureReadWrite.Default)
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

        /// <summary>An eye's image, centred in its box, filling the given shares of the box's width and height.</summary>
        void PlaceEye(RawImage image, float centre, float widthShare, float heightShare)
        {
            var rect = image.rectTransform;
            Vector2 half = new(eyeSize.x * widthShare / 2f, eyeSize.y * heightShare / 2f);
            rect.anchorMin = new Vector2(centre, 0.5f) - half;
            rect.anchorMax = new Vector2(centre, 0.5f) + half;
        }

        /// <summary>
        /// The camera picture's width over height as shown on this screen (4:3 on iPhone, landscape),
        /// or null when the camera hasn't reported its resolution yet (the eye then fills its box).
        /// </summary>
        float? PictureAspect()
        {
            if (cameraManager == null || !cameraManager.TryGetIntrinsics(out var intrinsics) ||
                intrinsics.resolution.x <= 0 || intrinsics.resolution.y <= 0)
                return null;
            float aspect = (float)intrinsics.resolution.x / intrinsics.resolution.y;  // the sensor's own, landscape
            bool portrait = Screen.orientation is ScreenOrientation.Portrait or ScreenOrientation.PortraitUpsideDown;
            return portrait ? 1f / aspect : aspect;
        }

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
