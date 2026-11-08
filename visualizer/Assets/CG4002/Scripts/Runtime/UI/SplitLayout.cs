using System;
using CG4002.AR;
using UnityEngine;
using UnityEngine.Serialization;

namespace CG4002.UI
{
    /// <summary>
    /// The HUD's layout for split screen: each eye is much narrower than the screen (about as wide as
    /// it is tall), so the corner layout is rearranged to use the eye's full width. While SplitScreen
    /// is active, each listed element moves to (and is scaled to) its split place, stretches across
    /// the eye, or is hidden (the buttons, which can't be tapped in a headset); afterwards it goes back
    /// to where it was. Positions are set up by HudBuilder.
    ///
    /// Applied every frame while split, since the eye's width (ViewArea) can change with the screen.
    /// </summary>
    public class SplitLayout : MonoBehaviour
    {
        [Serializable]
        public class Placement
        {
            public RectTransform target;
            public bool hide;
            public Vector2 anchor;           // also the pivot
            public Vector2 anchoredPosition;
            public float scale = 1f;
            [Tooltip("Stretch across the eye's width, `margin` in from each side (anchor.y still sets the height).")]
            public bool stretch;
            public float margin;
            [Tooltip("If above 0: the element's width at scale 1. The scale is then capped so it takes at most " +
                     "`widthShare` of the eye's width (so two side by side never overlap on narrower screens).")]
            public float fitWidth;
            public float widthShare = 0.5f;
        }

        [SerializeField, FormerlySerializedAs("compact")] Placement[] split = Array.Empty<Placement>();

        (Vector2 min, Vector2 max, Vector2 pivot, Vector2 position, Vector2 size, Vector3 scale, bool active)[] normal;
        bool applied;

        void Awake()
        {
            normal = new (Vector2, Vector2, Vector2, Vector2, Vector2, Vector3, bool)[split.Length];
            for (int i = 0; i < split.Length; i++)
            {
                var t = split[i].target;
                normal[i] = (t.anchorMin, t.anchorMax, t.pivot, t.anchoredPosition, t.sizeDelta, t.localScale, t.gameObject.activeSelf);
            }
        }

        void OnDisable()
        {
            if (applied)
                Restore();
        }

        void LateUpdate()
        {
            if (SplitScreen.Active)
                Apply();
            else if (applied)
                Restore();
        }

        void Apply()
        {
            applied = true;
            for (int i = 0; i < split.Length; i++)
            {
                var placement = split[i];
                var t = placement.target;
                if (placement.hide)
                {
                    t.gameObject.SetActive(false);
                    continue;
                }
                float scale = placement.scale;
                if (placement.fitWidth > 0f && t.parent is RectTransform area)
                    scale = Mathf.Min(scale, placement.widthShare * area.rect.width / placement.fitWidth);

                if (placement.stretch)
                {
                    t.anchorMin = new Vector2(0f, placement.anchor.y);
                    t.anchorMax = new Vector2(1f, placement.anchor.y);
                    t.pivot = new Vector2(0.5f, placement.anchor.y);
                    // Stretched in the parent's units; divided by the scale so the scaled width still fits.
                    float parentWidth = t.parent is RectTransform parent ? parent.rect.width : 0f;
                    t.sizeDelta = new Vector2(parentWidth / scale - parentWidth - 2f * placement.margin / scale, normal[i].size.y);
                }
                else
                {
                    t.anchorMin = t.anchorMax = t.pivot = placement.anchor;
                }
                t.anchoredPosition = placement.anchoredPosition;
                t.localScale = Vector3.one * scale;
            }
        }

        void Restore()
        {
            applied = false;
            for (int i = 0; i < split.Length; i++)
            {
                var t = split[i].target;
                var (min, max, pivot, position, size, scale, active) = normal[i];
                t.anchorMin = min;
                t.anchorMax = max;
                t.pivot = pivot;
                t.anchoredPosition = position;
                t.sizeDelta = size;
                t.localScale = scale;
                if (split[i].hide)
                    t.gameObject.SetActive(active);  // only what this hid; others may be shown or hidden by the HUD
            }
        }
    }
}
