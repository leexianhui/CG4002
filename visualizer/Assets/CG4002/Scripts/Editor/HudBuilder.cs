using CG4002.AR;
using CG4002.State;
using CG4002.UI;
using UnityEditor;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.InputSystem.UI;
using UnityEngine.UI;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Builds the HUD layout in the open scene and wires it to a HudController. Called by
    /// GameSceneSetup. Rebuilds from scratch each time, so move things in the editor only after
    /// the last setup run, or change the numbers here.
    ///
    ///   HUD (Canvas + GraphicRaycaster, 1920x1080 reference)
    ///   ├─ You          top-left:  label, health bar, attack-ready bar, weapon
    ///   ├─ Opponent     top-right: label, health bar, GUARDING
    ///   ├─ Outcome      centre:    "HIT -10" / "BLOCKED", shown briefly
    ///   ├─ Result       centre:    "DUMMY DEFEATED"
    ///   └─ Debug        bottom-right: RESET, MOVE DUMMY, TRACKING buttons (DebugPanel)
    ///   WeaponSelect (Canvas + WeaponSelectScreen): title and instruction, top centre; the
    ///                   dummies themselves are spawned in the world at runtime
    ///   EventSystem     routes taps to the buttons
    /// </summary>
    static class HudBuilder
    {
        // Kept away from the screen edges (report 6.1.3).
        const float Margin = 60f;
        const float PanelWidth = 520f;

        static readonly Color SelfColour = new(0.25f, 0.7f, 1f);
        static readonly Color OpponentColour = new(1f, 0.35f, 0.25f);
        static readonly Color BarBackground = new(0f, 0f, 0f, 0.55f);

        public static void Build(GameState gameState, MessageDispatcher dispatcher,
            DummyPlacer placer, TrackedImageDebug trackingOverlay)
        {
            var existing = GameObject.Find("HUD");
            if (existing != null)
                Object.DestroyImmediate(existing);
            EnsureEventSystem();

            // GraphicRaycaster finds the button under a tap; without it buttons never get presses.
            var canvasObject = new GameObject("HUD", typeof(Canvas), typeof(CanvasScaler), typeof(GraphicRaycaster));
            var canvas = canvasObject.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            var scaler = canvasObject.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1920f, 1080f);
            scaler.matchWidthOrHeight = 0.5f;
            var root = canvasObject.transform;

            // You: top-left.
            var self = Panel(root, "You", anchorX: 0f);
            Label(self, "Label", "YOU", 34, TextAnchor.UpperLeft, y: 0f, SelfColour);
            var selfHealth = Bar(self, "Health", y: -50f, height: 34f, SelfColour);
            var selfHealthText = Label(self, "HealthText", "100 / 100", 26, TextAnchor.UpperLeft, y: -92f, Color.white);
            Label(self, "CooldownLabel", "ATTACK", 20, TextAnchor.UpperLeft, y: -132f, Color.white);
            var cooldown = Bar(self, "Cooldown", y: -160f, height: 12f, Color.white);
            var weapon = Label(self, "Weapon", "SWORD & SHIELD", 24, TextAnchor.UpperLeft, y: -186f, Color.white);

            // Opponent: top-right.
            var opponent = Panel(root, "Opponent", anchorX: 1f);
            Label(opponent, "Label", "DUMMY", 34, TextAnchor.UpperRight, y: 0f, OpponentColour);
            var opponentHealth = Bar(opponent, "Health", y: -50f, height: 34f, OpponentColour);
            var opponentHealthText = Label(opponent, "HealthText", "100 / 100", 26, TextAnchor.UpperRight, y: -92f, Color.white);
            var guard = Label(opponent, "Guarding", "GUARDING", 28, TextAnchor.UpperRight, y: -132f, new Color(1f, 0.85f, 0.2f));

            // Centre messages.
            var outcome = CentreText(root, "Outcome", 72, y: 120f);
            var result = CentreText(root, "Result", 96, y: 0f);

            var hud = canvasObject.AddComponent<HudController>();
            var settings = new SerializedObject(hud);
            settings.FindProperty("gameState").objectReferenceValue = gameState;
            settings.FindProperty("dispatcher").objectReferenceValue = dispatcher;
            settings.FindProperty("selfHealthFill").objectReferenceValue = selfHealth;
            settings.FindProperty("selfHealthText").objectReferenceValue = selfHealthText;
            settings.FindProperty("cooldownFill").objectReferenceValue = cooldown;
            settings.FindProperty("weaponText").objectReferenceValue = weapon;
            settings.FindProperty("opponentHealthFill").objectReferenceValue = opponentHealth;
            settings.FindProperty("opponentHealthText").objectReferenceValue = opponentHealthText;
            settings.FindProperty("opponentGuardIndicator").objectReferenceValue = guard.gameObject;
            settings.FindProperty("outcomeText").objectReferenceValue = outcome;
            settings.FindProperty("resultText").objectReferenceValue = result;
            settings.ApplyModifiedPropertiesWithoutUndo();

            BuildDebugPanel(root, gameState, dispatcher, placer, trackingOverlay);
        }

        /// <summary>
        /// The weapon-selection screen, wired to what's already in the open scene (GameState,
        /// DummyPlacer and its camera and planes, the HUD). Rebuilt from scratch each time.
        /// </summary>
        public static WeaponSelectScreen BuildWeaponSelect()
        {
            var existing = GameObject.Find("WeaponSelect");
            if (existing != null)
                Object.DestroyImmediate(existing);

            var gameState = Object.FindAnyObjectByType<GameState>();
            var placer = Object.FindAnyObjectByType<DummyPlacer>();
            var hud = Object.FindAnyObjectByType<HudController>();
            if (gameState == null || placer == null || hud == null)
                throw new System.InvalidOperationException("Run CG4002 > Setup Game Scene first: no GameState, DummyPlacer or HUD in the scene.");
            var placement = new SerializedObject(placer);

            var canvasObject = new GameObject("WeaponSelect", typeof(Canvas), typeof(CanvasScaler));
            var canvas = canvasObject.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 1;
            var scaler = canvasObject.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1920f, 1080f);
            scaler.matchWidthOrHeight = 0.5f;

            var title = CentreText(canvasObject.transform, "Title", 72, y: 0f);
            var instruction = CentreText(canvasObject.transform, "Instruction", 40, y: 0f);
            foreach (var (text, y) in new[] { (title, -Margin), (instruction, -Margin - 100f) })
            {
                var rect = text.rectTransform;
                rect.anchorMin = rect.anchorMax = rect.pivot = new Vector2(0.5f, 1f);
                rect.anchoredPosition = new Vector2(0f, y);
            }

            var screen = canvasObject.AddComponent<WeaponSelectScreen>();
            var settings = new SerializedObject(screen);
            settings.FindProperty("gameState").objectReferenceValue = gameState;
            settings.FindProperty("catalog").objectReferenceValue = LoadGenerated<CG4002.Models.WeaponCatalog>("Definitions/WeaponCatalog.asset");
            settings.FindProperty("dummyPrefab").objectReferenceValue = LoadGenerated<CG4002.Models.AvatarView>("Prefabs/OpponentAvatar.prefab");
            settings.FindProperty("hologram").objectReferenceValue = LoadGenerated<Material>("Materials/M_Hologram.mat");
            settings.FindProperty("arCamera").objectReferenceValue = placement.FindProperty("arCamera").objectReferenceValue;
            settings.FindProperty("planeManager").objectReferenceValue = placement.FindProperty("planeManager").objectReferenceValue;
            settings.FindProperty("placer").objectReferenceValue = placer;
            settings.FindProperty("hud").objectReferenceValue = hud.GetComponent<Canvas>();
            settings.FindProperty("title").objectReferenceValue = title;
            settings.FindProperty("instruction").objectReferenceValue = instruction;
            settings.ApplyModifiedPropertiesWithoutUndo();
            return screen;
        }

        static T LoadGenerated<T>(string path) where T : Object =>
            AssetDatabase.LoadAssetAtPath<T>($"{AssetIO.Root}/{path}")
            ?? throw new System.InvalidOperationException($"Missing {AssetIO.Root}/{path}: run CG4002 > Generate Models.");

        /// <summary>Three stacked buttons in the bottom-right corner.</summary>
        static void BuildDebugPanel(Transform root, GameState gameState, MessageDispatcher dispatcher,
            DummyPlacer placer, TrackedImageDebug trackingOverlay)
        {
            var panel = NewRect(root, "Debug");
            panel.anchorMin = panel.anchorMax = panel.pivot = new Vector2(1f, 0f);
            panel.anchoredPosition = new Vector2(-Margin, Margin);
            panel.sizeDelta = new Vector2(320f, 3 * 90f);

            var reset = DebugButton(panel, "Reset", "RESET", y: 180f);
            var moveDummy = DebugButton(panel, "MoveDummy", "MOVE DUMMY", y: 90f);
            var tracking = DebugButton(panel, "Tracking", "TRACKING: ON", y: 0f);

            var debugPanel = panel.gameObject.AddComponent<DebugPanel>();
            var settings = new SerializedObject(debugPanel);
            settings.FindProperty("dispatcher").objectReferenceValue = dispatcher;
            settings.FindProperty("gameState").objectReferenceValue = gameState;
            settings.FindProperty("placer").objectReferenceValue = placer;
            settings.FindProperty("trackingOverlay").objectReferenceValue = trackingOverlay;
            settings.FindProperty("resetButton").objectReferenceValue = reset;
            settings.FindProperty("moveDummyButton").objectReferenceValue = moveDummy;
            settings.FindProperty("trackingButton").objectReferenceValue = tracking;
            settings.FindProperty("trackingLabel").objectReferenceValue = tracking.GetComponentInChildren<Text>();
            settings.ApplyModifiedPropertiesWithoutUndo();
        }

        static Button DebugButton(RectTransform panel, string name, string label, float y)
        {
            var rect = NewRect(panel, name);
            rect.anchorMin = new Vector2(0f, 0f);
            rect.anchorMax = new Vector2(1f, 0f);
            rect.pivot = new Vector2(0.5f, 0f);
            rect.anchoredPosition = new Vector2(0f, y);
            rect.sizeDelta = new Vector2(0f, 76f);
            rect.gameObject.AddComponent<Image>().color = BarBackground;
            var button = rect.gameObject.AddComponent<Button>();

            var text = NewRect(rect, "Label");
            text.anchorMin = Vector2.zero;
            text.anchorMax = Vector2.one;
            text.offsetMin = text.offsetMax = Vector2.zero;
            AddText(text, label, 28, TextAnchor.MiddleCenter, Color.white);
            return button;
        }

        /// <summary>Buttons need an EventSystem to receive taps; this one reads the new Input System.</summary>
        static void EnsureEventSystem()
        {
            if (Object.FindAnyObjectByType<EventSystem>() != null)
                return;
            new GameObject("EventSystem", typeof(EventSystem), typeof(InputSystemUIInputModule));
        }

        /// <summary>A column pinned to the top-left (anchorX 0) or top-right (anchorX 1) corner.</summary>
        static RectTransform Panel(Transform parent, string name, float anchorX)
        {
            var rect = NewRect(parent, name);
            rect.anchorMin = rect.anchorMax = rect.pivot = new Vector2(anchorX, 1f);
            rect.anchoredPosition = new Vector2(anchorX == 0f ? Margin : -Margin, -Margin);
            rect.sizeDelta = new Vector2(PanelWidth, 240f);
            return rect;
        }

        /// <summary>A background strip with a coloured fill; returns the fill, which HudController resizes.</summary>
        static RectTransform Bar(RectTransform panel, string name, float y, float height, Color colour)
        {
            var background = NewRect(panel, name);
            StretchRow(background, y, height);
            background.gameObject.AddComponent<Image>().color = BarBackground;

            var fill = NewRect(background, "Fill");
            fill.anchorMin = Vector2.zero;
            fill.anchorMax = Vector2.one;
            fill.offsetMin = fill.offsetMax = Vector2.zero;
            fill.pivot = new Vector2(0f, 0.5f);
            fill.gameObject.AddComponent<Image>().color = colour;
            return fill;
        }

        static Text Label(RectTransform panel, string name, string text, int size, TextAnchor alignment, float y, Color colour)
        {
            var rect = NewRect(panel, name);
            StretchRow(rect, y, size * 1.4f);
            return AddText(rect, text, size, alignment, colour);
        }

        static Text CentreText(Transform parent, string name, int size, float y)
        {
            var rect = NewRect(parent, name);
            rect.anchorMin = rect.anchorMax = rect.pivot = new Vector2(0.5f, 0.5f);
            rect.anchoredPosition = new Vector2(0f, y);
            rect.sizeDelta = new Vector2(1400f, size * 1.4f);
            var text = AddText(rect, "", size, TextAnchor.MiddleCenter, Color.white);
            // A dark outline keeps white text readable over a bright camera image.
            text.gameObject.AddComponent<Outline>().effectColor = new Color(0f, 0f, 0f, 0.8f);
            return text;
        }

        static Text AddText(RectTransform rect, string content, int size, TextAnchor alignment, Color colour)
        {
            var text = rect.gameObject.AddComponent<Text>();
            text.font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
            text.text = content;
            text.fontSize = size;
            text.fontStyle = FontStyle.Bold;
            text.alignment = alignment;
            text.color = colour;
            text.horizontalOverflow = HorizontalWrapMode.Overflow;
            text.raycastTarget = false;
            return text;
        }

        /// <summary>Full panel width, fixed height, placed y pixels below the panel's top.</summary>
        static void StretchRow(RectTransform rect, float y, float height)
        {
            rect.anchorMin = new Vector2(0f, 1f);
            rect.anchorMax = new Vector2(1f, 1f);
            rect.pivot = new Vector2(0.5f, 1f);
            rect.anchoredPosition = new Vector2(0f, y);
            rect.sizeDelta = new Vector2(0f, height);
        }

        static RectTransform NewRect(Transform parent, string name)
        {
            var go = new GameObject(name, typeof(RectTransform));
            go.transform.SetParent(parent, false);
            return (RectTransform)go.transform;
        }
    }
}
