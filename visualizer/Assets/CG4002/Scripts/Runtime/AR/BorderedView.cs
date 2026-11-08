using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace CG4002.AR
{
    /// <summary>
    /// Draws the AR view slightly smaller than the screen, centred, with a thin black border all
    /// round, so in the headset the whole view (HUD included, see UI/ViewArea) sits inside the lens.
    /// Same shape as the screen, so the camera picture is cropped exactly as it would be full-screen.
    ///
    /// AR Foundation computes the camera picture's crop and the 3D projection for the whole screen.
    /// Just before the camera renders, this asks the camera subsystem for the same two matrices
    /// computed for the smaller viewport and applies them, so the picture is never squashed and
    /// virtual objects stay lined up with the real ones whatever shape the viewport has.
    ///
    /// Screen-space taps must account for the border: use Camera.ScreenPointToRay (see DummyPlacer).
    ///
    /// Split screen (SplitScreen) renders the camera into an eye-sized texture instead: then the view
    /// fills that texture (no border) and the matrices are computed for the eye's shape.
    /// </summary>
    [RequireComponent(typeof(Camera))]
    public class BorderedView : MonoBehaviour
    {
        // The camera background shader's display matrix (AR Foundation's RenderingUtility.k_DisplayTransformName).
        static readonly int DisplayTransformId = Shader.PropertyToID("_UnityDisplayTransform");

        [Tooltip("How much of the screen's width and height the view fills; the rest is black border.")]
        [SerializeField, Range(0.5f, 1f)] float scale = 0.9f;

        Camera arCamera;
        ARCameraManager cameraManager;
        ARCameraBackground background;

        void Awake()
        {
            arCamera = GetComponent<Camera>();
            cameraManager = GetComponent<ARCameraManager>();
            background = GetComponent<ARCameraBackground>();
        }

        void OnEnable() => RenderPipelineManager.beginCameraRendering += OnBeginCameraRendering;

        void OnDisable()
        {
            RenderPipelineManager.beginCameraRendering -= OnBeginCameraRendering;
            arCamera.rect = new Rect(0f, 0f, 1f, 1f);
        }

        void OnBeginCameraRendering(ScriptableRenderContext context, Camera rendering)
        {
            if (rendering != arCamera || cameraManager == null || cameraManager.subsystem == null)
                return;
            arCamera.rect = arCamera.targetTexture != null
                ? new Rect(0f, 0f, 1f, 1f)
                : new Rect((1f - scale) / 2f, (1f - scale) / 2f, scale, scale);

            var parameters = new XRCameraParams
            {
                zNear = arCamera.nearClipPlane,
                zFar = arCamera.farClipPlane,
                screenWidth = arCamera.pixelWidth,
                screenHeight = arCamera.pixelHeight,
                screenOrientation = Screen.orientation,
            };
            if (!cameraManager.subsystem.TryGetLatestFrame(parameters, out var frame))
                return;
            if (frame.TryGetProjectionMatrix(out var projection))
                arCamera.projectionMatrix = projection;
            if (frame.TryGetDisplayMatrix(out var display) && background != null && background.material != null)
                background.material.SetMatrix(DisplayTransformId, display);
        }
    }
}
