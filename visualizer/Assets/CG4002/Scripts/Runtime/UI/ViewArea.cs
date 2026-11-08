using UnityEngine;

namespace CG4002.UI
{
    /// <summary>
    /// Keeps screen-space UI inside the AR camera's view area. BorderedView draws the camera into a
    /// smaller centred area with a black border; HUD content parented under this rect follows that
    /// area and shrinks with it, so the whole picture, HUD included, is one smaller screen.
    ///
    /// Children are laid out as if the area were a screen 1080 units tall (its width follows the
    /// area's shape on screen), then scaled to the area's real size.
    ///
    /// Split screen (AR/SplitScreen) renders these canvases into an eye-sized texture, whose shape
    /// differs from the canvas's own (the canvas keeps the screen's proportions and is squeezed
    /// sideways into the eye). The width is measured in real pixels and the horizontal scale
    /// compensates, so the HUD keeps its true proportions in each eye.
    /// </summary>
    [RequireComponent(typeof(RectTransform))]
    public class ViewArea : MonoBehaviour
    {
        public const float ReferenceHeight = 1080f;

        [SerializeField] Camera arCamera;

        RectTransform area;
        RectTransform canvas;

        void Awake()
        {
            area = (RectTransform)transform;
            canvas = (RectTransform)GetComponentInParent<Canvas>().rootCanvas.transform;
        }

        void LateUpdate()
        {
            Rect view = arCamera != null ? arCamera.rect : new Rect(0f, 0f, 1f, 1f);
            Vector2 canvasSize = canvas.rect.size;
            Vector2 areaSize = new(view.width * canvasSize.x, view.height * canvasSize.y);  // canvas units
            if (areaSize.y <= 0f)
                return;

            // The area's true shape in pixels: the camera's viewport on screen, or the whole eye texture.
            float pixelAspect = arCamera != null && arCamera.pixelHeight > 0
                ? (float)arCamera.pixelWidth / arCamera.pixelHeight
                : areaSize.x / areaSize.y;
            float referenceWidth = ReferenceHeight * pixelAspect;

            area.anchorMin = area.anchorMax = view.center;
            area.pivot = new Vector2(0.5f, 0.5f);
            area.anchoredPosition = Vector2.zero;
            area.sizeDelta = new Vector2(referenceWidth, ReferenceHeight);
            // Equal on screen (the two scales match); different when the canvas is squeezed into an eye.
            area.localScale = new Vector3(areaSize.x / referenceWidth, areaSize.y / ReferenceHeight, 1f);
        }
    }
}
