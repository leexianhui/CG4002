using CG4002.AR;
using CG4002.Combat;
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
    ///   HUD (Canvas + GraphicRaycaster)
    ///   └─ View           ViewArea: follows the AR camera's view area (smaller than the screen, with a
    ///      │              border; see BorderedView) and scales with it, laid out 1080 units tall
    ///      ├─ You          top-left:  YOU, gold-framed health bar (HP inside), weapon
    ///      ├─ Cooldowns    bottom-left: an icon per move (the six the AI recognises) with a grey radial sweep
    ///      ├─ Opponent     top-right: ENEMY, gold-framed health bar, GUARDING
    ///      ├─ Outcome      centre:    "HIT -10" / "BLOCKED", shown briefly
    ///      ├─ Guarding     centre:    GUARDING while your guard is up (same spot; hit messages take over briefly)
    ///      ├─ Result       centre:    "ENEMY DEFEATED"
    ///      ├─ GodMode      bottom centre: GOD MODE while the engine's god mode is on
    ///      └─ Debug        bottom-right: RESET, MOVE DUMMY, TRACKING buttons (DebugPanel)
    ///   WeaponSelect (Canvas + WeaponSelectScreen) └─ View: title and instruction (top centre while
    ///                   choosing, centre for calibration / status messages), and demo mode's CHOOSE
    ///                   buttons; the dummies are spawned in the world at runtime
    ///   DemoControls (Canvas + DemoMode, OnlineScene only) └─ View: demo mode's control bar, bottom-left
    ///   FirstPerson  (Canvas + FirstPersonAttack) └─ View: the recognised move's name, lower centre;
    ///                   the first-person avatar is spawned under the AR camera at runtime
    ///   ReplaceDummy (Canvas + ReplaceDummyButton, OnlineScene only): RE-PLACE DUMMY, bottom centre,
    ///                   from placing until calibrated; drawn over split screen, never in the eyes
    ///   EventSystem     routes taps to the buttons
    /// The HUD also has a SplitLayout (its layout while split screen is on), and the AR
    /// camera a SplitScreen that shows the HUD, FirstPerson and WeaponSelect canvases in both eyes.
    /// </summary>
    static class HudBuilder
    {
        // Kept away from the screen edges (report 6.1.3).
        const float Margin = 60f;
        const float PanelWidth = 520f;
        // The YOU / ENEMY panels (health bars) sit a little lower and further in than the corners,
        // nearer the middle of the lens.
        static readonly Vector2 TopPanelInset = new(140f, 100f);

        // Medieval theme: gold names and frames, parchment text, green health over a dark red empty bar.
        static readonly Color Gold = new(0.96f, 0.82f, 0.48f);
        static readonly Color EnemyColour = new(0.95f, 0.5f, 0.32f);
        static readonly Color Parchment = new(0.95f, 0.9f, 0.78f);
        static readonly Color HealthColour = new(0.47f, 0.88f, 0.35f);
        static readonly Color EmptyColour = new(0.43f, 0.07f, 0.05f);
        static readonly Color IconColour = new(0.94f, 0.8f, 0.47f);
        static readonly Color CooldownShade = new(0.08f, 0.08f, 0.08f, 0.72f);

        // HUD art (Assets/CG4002/UI, drawn by Tools/make_hud_art.py; icons from game-icons.net, font Cinzel).
        const string ArtDir = "Assets/CG4002/UI";
        static Font HudFont => AssetDatabase.LoadAssetAtPath<Font>($"{ArtDir}/Fonts/Cinzel-Bold.ttf");

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
            var root = ViewRoot(canvasObject, ArCamera(placer));

            // You: top-left.
            var self = Panel(root, "You", anchorX: 0f);
            Label(self, "Label", "YOU", 32, TextAnchor.UpperLeft, y: 0f, Gold);
            var (selfHealth, selfHealthText) = HealthBar(self, anchorX: 0f, y: -44f);
            var weapon = Label(self, "Weapon", "SWORD & SHIELD", 24, TextAnchor.UpperLeft, y: -116f, Parchment);

            // Opponent: top-right.
            var opponent = Panel(root, "Opponent", anchorX: 1f);
            Label(opponent, "Label", "ENEMY", 32, TextAnchor.UpperRight, y: 0f, EnemyColour);
            var (opponentHealth, opponentHealthText) = HealthBar(opponent, anchorX: 1f, y: -44f);
            var guard = Label(opponent, "Guarding", "GUARDING", 26, TextAnchor.UpperRight, y: -116f, Gold);

            // Centre messages.
            var outcome = CentreText(root, "Outcome", 72, y: 120f);
            // Your own guard: same place and size as the hit / miss messages (which take over briefly).
            var selfGuard = CentreText(root, "Guarding", 72, y: 120f);
            selfGuard.text = "GUARDING";
            selfGuard.color = Gold;
            var result = CentreText(root, "Result", 96, y: 0f);
            result.color = Gold;
            // God mode (the engine's, for both players): bottom centre, clear of the cooldown row.
            var godMode = CentreText(root, "GodMode", 40, y: 0f);
            godMode.text = "GOD MODE";
            godMode.color = Gold;
            var godModeRect = godMode.rectTransform;
            godModeRect.anchorMin = godModeRect.anchorMax = godModeRect.pivot = new Vector2(0.5f, 0f);
            godModeRect.anchoredPosition = new Vector2(0f, 40f);
            godMode.gameObject.SetActive(false);

            var hud = canvasObject.AddComponent<HudController>();
            var settings = new SerializedObject(hud);
            settings.FindProperty("gameState").objectReferenceValue = gameState;
            settings.FindProperty("dispatcher").objectReferenceValue = dispatcher;
            settings.FindProperty("selfHealthFill").objectReferenceValue = selfHealth;
            settings.FindProperty("selfHealthText").objectReferenceValue = selfHealthText;
            settings.FindProperty("weaponText").objectReferenceValue = weapon;
            settings.FindProperty("selfGuardIndicator").objectReferenceValue = selfGuard.gameObject;
            settings.FindProperty("opponentHealthFill").objectReferenceValue = opponentHealth;
            settings.FindProperty("opponentHealthText").objectReferenceValue = opponentHealthText;
            settings.FindProperty("opponentGuardIndicator").objectReferenceValue = guard.gameObject;
            settings.FindProperty("outcomeText").objectReferenceValue = outcome;
            settings.FindProperty("resultText").objectReferenceValue = result;
            settings.FindProperty("godModeBadge").objectReferenceValue = godMode.gameObject;
            BuildCooldowns(root, settings.FindProperty("cooldownRows"));
            settings.ApplyModifiedPropertiesWithoutUndo();

            BuildDebugPanel(root, gameState, dispatcher, placer, trackingOverlay);
            BuildSplitLayout(canvasObject, root, self, opponent, outcome, selfGuard, result, godModeRect);
        }

        /// <summary>
        /// The HUD's layout while split screen is on (UI/SplitLayout). Each eye is about as wide as it
        /// is tall, so the layout spreads across the eye's whole width: the health panels sit a little
        /// in from its top corners, about 0.8x full view with a clear gap between them, the cooldown
        /// icons spread along the bottom edge (moderately: about half a full spread's gaps), the centre
        /// messages shrink to fit, and the buttons are
        /// hidden.
        /// </summary>
        static void BuildSplitLayout(GameObject canvasObject, RectTransform root, RectTransform self,
            RectTransform opponent, Text outcome, Text selfGuard, Text result, RectTransform godMode)
        {
            var layout = canvasObject.AddComponent<SplitLayout>();
            const float edge = 24f;           // the cooldown row, from the eye's sides
            // The cooldown row's width, from packed (0) to the eye's full width (1): 0.4 gives about
            // half the gaps of a full spread, keeping the icons together in the middle.
            const float cooldownSpread = 0.4f;
            const float healthBarWidth = 440f; // HealthBar's width at scale 1
            // The health panels sit in from the eye's corners, toward the middle of the lens.
            var healthInset = new Vector2(100f, 130f);
            var placements = new (RectTransform target, bool hide, Vector2 anchor, Vector2 position, float scale,
                bool stretch, float fitWidth, float widthShare)[]
            {
                // Each health bar takes up to 34% of the eye's width (about 0.8x full view on an iPhone,
                // leaving a clear gap between the two), and up to 0.88x where the eye is wider.
                (self, false, new Vector2(0f, 1f), new Vector2(healthInset.x, -healthInset.y), 0.88f, false, healthBarWidth, 0.344f),
                (opponent, false, new Vector2(1f, 1f), new Vector2(-healthInset.x, -healthInset.y), 0.88f, false, healthBarWidth, 0.344f),
                ((RectTransform)root.Find("Cooldowns"), false, new Vector2(0.5f, 0f), new Vector2(0f, 140f), 1f, true, 0f, 0f),
                (outcome.rectTransform, false, new Vector2(0.5f, 0.5f), new Vector2(0f, 120f), 0.8f, false, 0f, 0f),
                (selfGuard.rectTransform, false, new Vector2(0.5f, 0.5f), new Vector2(0f, 120f), 0.8f, false, 0f, 0f),
                (result.rectTransform, false, new Vector2(0.5f, 0.5f), Vector2.zero, 0.75f, false, 0f, 0f),
                // Above the cooldown row (140 up, icons 108 tall).
                (godMode, false, new Vector2(0.5f, 0f), new Vector2(0f, 270f), 0.8f, false, 0f, 0f),
                ((RectTransform)root.Find("Debug"), true, Vector2.zero, Vector2.zero, 1f, false, 0f, 0f),
            };
            var settings = new SerializedObject(layout);
            var list = settings.FindProperty("split");
            list.arraySize = placements.Length;
            for (int i = 0; i < placements.Length; i++)
            {
                var (target, hide, anchor, position, scale, stretch, fitWidth, widthShare) = placements[i];
                var entry = list.GetArrayElementAtIndex(i);
                entry.FindPropertyRelative("target").objectReferenceValue = target;
                entry.FindPropertyRelative("hide").boolValue = hide;
                entry.FindPropertyRelative("anchor").vector2Value = anchor;
                entry.FindPropertyRelative("anchoredPosition").vector2Value = position;
                entry.FindPropertyRelative("scale").floatValue = scale;
                entry.FindPropertyRelative("stretch").boolValue = stretch;
                entry.FindPropertyRelative("margin").floatValue = edge;
                entry.FindPropertyRelative("spread").floatValue = stretch ? cooldownSpread : 1f;
                entry.FindPropertyRelative("fitWidth").floatValue = fitWidth;
                entry.FindPropertyRelative("widthShare").floatValue = widthShare;
            }
            settings.ApplyModifiedPropertiesWithoutUndo();
        }

        /// <summary>
        /// Split screen (AR/SplitScreen) on the AR camera, wired to the open scene: the placer, demo
        /// mode if present, and the canvases shown in both eyes. Run after
        /// the HUD, first-person view, weapon selection and demo controls are built.
        /// </summary>
        public static void WireSplitScreen()
        {
            var placer = Object.FindAnyObjectByType<DummyPlacer>();
            var camera = ArCamera(placer);
            if (!camera.TryGetComponent(out SplitScreen split))
                split = camera.gameObject.AddComponent<SplitScreen>();
            var settings = new SerializedObject(split);
            settings.FindProperty("placer").objectReferenceValue = placer;
            settings.FindProperty("demo").objectReferenceValue = Object.FindAnyObjectByType<CG4002.Offline.DemoMode>(FindObjectsInactive.Include);
            var names = new[] { "HUD", "FirstPerson", "WeaponSelect" };
            var list = settings.FindProperty("canvases");
            list.arraySize = 0;
            foreach (var name in names)
            {
                var found = GameObject.Find(name);
                if (found == null || !found.TryGetComponent(out Canvas canvas))
                    continue;
                list.arraySize++;
                list.GetArrayElementAtIndex(list.arraySize - 1).objectReferenceValue = canvas;
            }
            settings.ApplyModifiedPropertiesWithoutUndo();
        }

        /// <summary>
        /// RE-PLACE DUMMY (UI/ReplaceDummyButton), bottom centre on its own canvas above split screen's
        /// output, wired to the open scene's placer and online calibration. Only OnlineScene has a
        /// calibration; without one nothing is built. Rebuilt from scratch each time.
        /// </summary>
        public static void BuildReplaceDummy()
        {
            var existing = GameObject.Find("ReplaceDummy");
            if (existing != null)
                Object.DestroyImmediate(existing);
            var calibration = Object.FindAnyObjectByType<CG4002.Net.SharedArenaCalibration>(FindObjectsInactive.Include);
            var placer = Object.FindAnyObjectByType<DummyPlacer>();
            if (calibration == null || placer == null)
                return;

            var canvasObject = new GameObject("ReplaceDummy", typeof(Canvas), typeof(CanvasScaler), typeof(GraphicRaycaster));
            var canvas = canvasObject.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 1001;  // above split screen's output (1000)
            var scaler = canvasObject.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1920f, 1080f);
            scaler.matchWidthOrHeight = 0.5f;

            var button = FramedButton((RectTransform)canvasObject.transform, "Button", "RE-PLACE DUMMY", 34);
            var rect = (RectTransform)button.transform;
            rect.anchorMin = rect.anchorMax = rect.pivot = new Vector2(0.5f, 0f);
            rect.anchoredPosition = new Vector2(0f, 70f);
            rect.sizeDelta = new Vector2(440f, 96f);
            button.gameObject.SetActive(false);

            var replace = canvasObject.AddComponent<ReplaceDummyButton>();
            var settings = new SerializedObject(replace);
            settings.FindProperty("placer").objectReferenceValue = placer;
            settings.FindProperty("calibration").objectReferenceValue = calibration;
            settings.FindProperty("button").objectReferenceValue = button;
            settings.ApplyModifiedPropertiesWithoutUndo();
            EnsureEventSystem();
        }

        /// <summary>
        /// The weapon-selection screen, wired to what's already in the open scene (GameState,
        /// DummyPlacer and its camera and planes, the online calibration if any, the HUD). Rebuilt from scratch each time.
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

            // GraphicRaycaster: demo mode's pick buttons take taps.
            var canvasObject = new GameObject("WeaponSelect", typeof(Canvas), typeof(CanvasScaler), typeof(GraphicRaycaster));
            var canvas = canvasObject.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 1;
            var scaler = canvasObject.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1920f, 1080f);
            scaler.matchWidthOrHeight = 0.5f;

            var view = ViewRoot(canvasObject, ArCamera(placer));
            var title = CentreText(view, "Title", 72, y: 0f);
            var instruction = CentreText(view, "Instruction", 40, y: 0f);
            title.color = Gold;
            instruction.color = Parchment;
            foreach (var (text, y) in new[] { (title, -Margin), (instruction, -Margin - 100f) })
            {
                var rect = text.rectTransform;
                rect.anchorMin = rect.anchorMax = rect.pivot = new Vector2(0.5f, 1f);
                rect.anchoredPosition = new Vector2(0f, y);
            }

            // Demo mode: a CHOOSE button per weapon, moved under its dummy at runtime.
            var weapons = new[] { "Spear", "SwordAndShield", "Mace" };  // WeaponSelectScreen.weapons' order
            var picks = new Button[weapons.Length];
            for (int i = 0; i < weapons.Length; i++)
            {
                picks[i] = FramedButton(view, $"Pick {weapons[i]}", "CHOOSE", 28);
                var rect = (RectTransform)picks[i].transform;
                rect.pivot = new Vector2(0.5f, 1f);  // hangs below the point it's placed at
                rect.sizeDelta = new Vector2(220f, 76f);
            }

            var screen = canvasObject.AddComponent<WeaponSelectScreen>();
            var settings = new SerializedObject(screen);
            var pickList = settings.FindProperty("pickButtons");
            pickList.arraySize = picks.Length;
            for (int i = 0; i < picks.Length; i++)
                pickList.GetArrayElementAtIndex(i).objectReferenceValue = picks[i];
            settings.FindProperty("gameState").objectReferenceValue = gameState;
            settings.FindProperty("catalog").objectReferenceValue = LoadGenerated<CG4002.Models.WeaponCatalog>("Definitions/WeaponCatalog.asset");
            settings.FindProperty("dummyPrefab").objectReferenceValue = LoadGenerated<CG4002.Models.AvatarView>("Prefabs/OpponentAvatar.prefab");
            settings.FindProperty("hologram").objectReferenceValue = LoadGenerated<Material>("Materials/M_Hologram.mat");
            settings.FindProperty("arCamera").objectReferenceValue = placement.FindProperty("arCamera").objectReferenceValue;
            settings.FindProperty("planeManager").objectReferenceValue = placement.FindProperty("planeManager").objectReferenceValue;
            settings.FindProperty("placer").objectReferenceValue = placer;
            settings.FindProperty("calibration").objectReferenceValue =
                Object.FindAnyObjectByType<CG4002.Net.SharedArenaCalibration>(FindObjectsInactive.Include);
            settings.FindProperty("hud").objectReferenceValue = hud.GetComponent<Canvas>();
            settings.FindProperty("title").objectReferenceValue = title;
            settings.FindProperty("instruction").objectReferenceValue = instruction;
            settings.FindProperty("labelFont").objectReferenceValue = HudFont;
            settings.ApplyModifiedPropertiesWithoutUndo();
            return screen;
        }

        /// <summary>
        /// The local player's first-person attack view, wired to the open scene (dispatcher, state,
        /// AR camera, prop tracker, FX). Rebuilt from scratch each time.
        /// </summary>
        public static CG4002.Controllers.FirstPersonAttack BuildFirstPerson()
        {
            var existing = GameObject.Find("FirstPerson");
            if (existing != null)
                Object.DestroyImmediate(existing);

            var dispatcher = Object.FindAnyObjectByType<MessageDispatcher>();
            var gameState = Object.FindAnyObjectByType<GameState>();
            var placer = Object.FindAnyObjectByType<DummyPlacer>();
            if (dispatcher == null || gameState == null || placer == null)
                throw new System.InvalidOperationException("Run CG4002 > Setup Game Scene first: no dispatcher, GameState or DummyPlacer in the scene.");

            var canvasObject = new GameObject("FirstPerson", typeof(Canvas), typeof(CanvasScaler));
            var canvas = canvasObject.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            var scaler = canvasObject.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1920f, 1080f);
            scaler.matchWidthOrHeight = 0.5f;
            var label = CentreText(ViewRoot(canvasObject, ArCamera(placer)), "MoveName", 56, y: -300f);
            label.color = Gold;

            var view = canvasObject.AddComponent<CG4002.Controllers.FirstPersonAttack>();
            var settings = new SerializedObject(view);
            settings.FindProperty("dispatcher").objectReferenceValue = dispatcher;
            settings.FindProperty("gameState").objectReferenceValue = gameState;
            settings.FindProperty("catalog").objectReferenceValue = LoadGenerated<CG4002.Models.WeaponCatalog>("Definitions/WeaponCatalog.asset");
            settings.FindProperty("avatarPrefab").objectReferenceValue = LoadGenerated<CG4002.Models.AvatarView>("Prefabs/OpponentAvatar.prefab");
            settings.FindProperty("arCamera").objectReferenceValue = new SerializedObject(placer).FindProperty("arCamera").objectReferenceValue;
            settings.FindProperty("prop").objectReferenceValue = Object.FindAnyObjectByType<PropTracker>();
            settings.FindProperty("fx").objectReferenceValue = Object.FindAnyObjectByType<CG4002.FX.CombatFxController>();
            settings.FindProperty("moveLabel").objectReferenceValue = label;
            settings.ApplyModifiedPropertiesWithoutUndo();
            return view;
        }

        /// <summary>
        /// Demo mode's control bar (CG4002.Offline.DemoMode), wired to the open scene and the given
        /// demo engine. Bottom-left, over the HUD's cooldown icons (which it replaces while on):
        ///   [ YOU | ENEMY ] [ CHANGE WEAPONS ]
        ///   [6 attack icons, same frames and sweeps as the cooldowns] [ GUARD ]
        /// Rebuilt from scratch each time; hidden until demo mode starts.
        /// </summary>
        public static CG4002.Offline.DemoMode BuildDemoControls(CG4002.Offline.LocalGameRules rules, WeaponSelectScreen weaponSelect)
        {
            var existing = GameObject.Find("DemoControls");
            if (existing != null)
                Object.DestroyImmediate(existing);

            var gameState = Object.FindAnyObjectByType<GameState>();
            var placer = Object.FindAnyObjectByType<DummyPlacer>();
            var hud = Object.FindAnyObjectByType<HudController>();
            if (gameState == null || placer == null || hud == null)
                throw new System.InvalidOperationException("Run CG4002 > Setup Game Scene first: no GameState, DummyPlacer or HUD in the scene.");

            var canvasObject = new GameObject("DemoControls", typeof(Canvas), typeof(CanvasScaler), typeof(GraphicRaycaster));
            var canvas = canvasObject.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 2;
            var scaler = canvasObject.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1920f, 1080f);
            scaler.matchWidthOrHeight = 0.5f;
            var view = ViewRoot(canvasObject, ArCamera(placer));

            const float size = 108f, gap = 12f, rowGap = 16f, switchHeight = 64f;
            var bar = NewRect(view, "Bar");
            bar.anchorMin = bar.anchorMax = bar.pivot = new Vector2(0f, 0f);
            bar.anchoredPosition = new Vector2(Margin, Margin);
            bar.sizeDelta = new Vector2(CooldownMoves.Length * (size + gap) + 220f, size + rowGap + switchHeight);

            Button BarButton(string name, string label, float x, float y, float width, float height, int fontSize)
            {
                var button = FramedButton(bar, name, label, fontSize);
                var rect = (RectTransform)button.transform;
                rect.anchorMin = rect.anchorMax = rect.pivot = new Vector2(0f, 0f);
                rect.anchoredPosition = new Vector2(x, y);
                rect.sizeDelta = new Vector2(width, height);
                return button;
            }

            float top = size + rowGap;
            var you = BarButton("You", "YOU", 0f, top, 150f, switchHeight, 26);
            var enemy = BarButton("Enemy", "ENEMY", 158f, top, 150f, switchHeight, 26);
            var changeWeapons = BarButton("ChangeWeapons", "CHANGE WEAPONS", 158f * 2f + rowGap, top, 300f, switchHeight, 24);

            var demo = canvasObject.AddComponent<CG4002.Offline.DemoMode>();
            var settings = new SerializedObject(demo);
            var attackList = settings.FindProperty("attacks");
            attackList.arraySize = CooldownMoves.Length;
            var inner = (min: new Vector2(22f / 160f, 22f / 160f), max: new Vector2(138f / 160f, 138f / 160f));
            for (int i = 0; i < CooldownMoves.Length; i++)
            {
                var (action, icon) = CooldownMoves[i];
                var slot = NewRect(bar, action.ToString());
                slot.anchorMin = slot.anchorMax = slot.pivot = new Vector2(0f, 0f);
                slot.anchoredPosition = new Vector2(i * (size + gap), 0f);
                slot.sizeDelta = new Vector2(size, size);
                var frame = Picture(slot, Art("Frames/IconFrame.png"), Color.white);
                frame.raycastTarget = true;  // the frame takes the tap
                var button = slot.gameObject.AddComponent<Button>();
                button.targetGraphic = frame;

                var glyph = NewRect(slot, "Icon");
                Inset(glyph, inner.min, inner.max);
                Picture(glyph, Art($"Icons/Icon_{icon}.png"), IconColour);
                var shade = NewRect(slot, "Cooldown");
                Inset(shade, inner.min, inner.max);
                var overlay = Picture(shade, Art("Frames/White.png"), CooldownShade);
                overlay.type = Image.Type.Filled;
                overlay.fillMethod = Image.FillMethod.Radial360;
                overlay.fillOrigin = (int)Image.Origin360.Top;
                overlay.fillClockwise = true;
                overlay.fillAmount = 0f;

                var entry = attackList.GetArrayElementAtIndex(i);
                entry.FindPropertyRelative("action").enumValueIndex = System.Array.IndexOf(System.Enum.GetValues(typeof(ActionType)), action);
                entry.FindPropertyRelative("button").objectReferenceValue = button;
                entry.FindPropertyRelative("overlay").objectReferenceValue = overlay;
            }

            // Hold to guard: a plain frame (no Button, so no click) with HoldButton.
            var guard = BarButton("Guard", "GUARD", CooldownMoves.Length * (size + gap), 0f, 200f, size, 32);
            var guardObject = guard.gameObject;
            Object.DestroyImmediate(guard);
            var hold = guardObject.AddComponent<HoldButton>();

            settings.FindProperty("gameState").objectReferenceValue = gameState;
            settings.FindProperty("rules").objectReferenceValue = rules;
            settings.FindProperty("placer").objectReferenceValue = placer;
            settings.FindProperty("weaponSelect").objectReferenceValue = weaponSelect;
            settings.FindProperty("hudCooldowns").objectReferenceValue = hud.transform.Find("View/Cooldowns")?.gameObject;
            settings.FindProperty("bar").objectReferenceValue = bar.gameObject;
            settings.FindProperty("youButton").objectReferenceValue = you;
            settings.FindProperty("enemyButton").objectReferenceValue = enemy;
            settings.FindProperty("changeWeaponsButton").objectReferenceValue = changeWeapons;
            settings.FindProperty("guardButton").objectReferenceValue = hold;
            settings.ApplyModifiedPropertiesWithoutUndo();
            return demo;
        }

        /// <summary>
        /// The container a canvas's content goes in: it follows the AR camera's view area (smaller
        /// than the screen, with a border) and scales with it, so the UI shrinks with the picture.
        /// </summary>
        static RectTransform ViewRoot(GameObject canvasObject, Camera arCamera)
        {
            var view = NewRect(canvasObject.transform, "View");
            view.sizeDelta = new Vector2(1920f, ViewArea.ReferenceHeight);
            var area = view.gameObject.AddComponent<ViewArea>();
            var settings = new SerializedObject(area);
            settings.FindProperty("arCamera").objectReferenceValue = arCamera;
            settings.ApplyModifiedPropertiesWithoutUndo();
            return view;
        }

        static Camera ArCamera(DummyPlacer placer) =>
            (Camera)new SerializedObject(placer).FindProperty("arCamera").objectReferenceValue;

        static T LoadGenerated<T>(string path) where T : Object =>
            AssetDatabase.LoadAssetAtPath<T>($"{AssetIO.Root}/{path}")
            ?? throw new System.InvalidOperationException($"Missing {AssetIO.Root}/{path}: run CG4002 > Generate Models.");

        // The six attacks the AI recognises, in the order the engine's vocabulary lists them.
        // Icons: game-icons.net (CC BY 3.0) saber-slash, sword-wound, crossed-slashes, stiletto,
        // sword-spin (Lorc) and sword-brandish (Delapouite). See Assets/CG4002/UI/CREDITS.md.
        static readonly (ActionType action, string icon)[] CooldownMoves =
        {
            (ActionType.HorizontalSwing, "Horizontal"), (ActionType.VerticalSwing, "Vertical"),
            (ActionType.DiagonalSwing, "Diagonal"), (ActionType.Thrust, "Thrust"),
            (ActionType.SpinAttack, "Spin"), (ActionType.Uppercut, "Uppercut"),
        };

        /// <summary>
        /// Bottom-left: a row of framed icons, one per move. A grey overlay covers the icon while the
        /// move cools down and sweeps away anticlockwise as it becomes ready (Image Filled, Radial360).
        /// </summary>
        static void BuildCooldowns(Transform root, SerializedProperty rows)
        {
            const float size = 108f, gap = 12f;
            var panel = NewRect(root, "Cooldowns");
            panel.anchorMin = panel.anchorMax = panel.pivot = new Vector2(0f, 0f);
            panel.anchoredPosition = new Vector2(Margin, Margin);
            panel.sizeDelta = new Vector2(CooldownMoves.Length * size + (CooldownMoves.Length - 1) * gap, size);

            // The icon and overlay sit inside the frame's gold border (22 of its 160 px each side).
            var inner = (min: new Vector2(22f / 160f, 22f / 160f), max: new Vector2(138f / 160f, 138f / 160f));
            rows.arraySize = CooldownMoves.Length;
            for (int i = 0; i < CooldownMoves.Length; i++)
            {
                var (action, icon) = CooldownMoves[i];
                var slot = NewRect(panel, action.ToString());
                // Anchored by fraction along the row, so at the row's own width the icons sit `gap`
                // apart, and when split screen stretches the row they spread evenly across it.
                float along = CooldownMoves.Length > 1 ? i / (CooldownMoves.Length - 1f) : 0f;
                slot.anchorMin = slot.anchorMax = slot.pivot = new Vector2(along, 0f);
                slot.anchoredPosition = Vector2.zero;
                slot.sizeDelta = new Vector2(size, size);
                Picture(slot, Art("Frames/IconFrame.png"), Color.white);

                var glyph = NewRect(slot, "Icon");
                Inset(glyph, inner.min, inner.max);
                Picture(glyph, Art($"Icons/Icon_{icon}.png"), IconColour);

                var shade = NewRect(slot, "Cooldown");
                Inset(shade, inner.min, inner.max);
                var overlay = Picture(shade, Art("Frames/White.png"), CooldownShade);
                overlay.type = Image.Type.Filled;
                overlay.fillMethod = Image.FillMethod.Radial360;
                overlay.fillOrigin = (int)Image.Origin360.Top;
                overlay.fillClockwise = true;  // as fillAmount falls, the edge sweeps back anticlockwise
                overlay.fillAmount = 0f;

                var row = rows.GetArrayElementAtIndex(i);
                row.FindPropertyRelative("action").enumValueIndex = System.Array.IndexOf(System.Enum.GetValues(typeof(ActionType)), action);
                row.FindPropertyRelative("overlay").objectReferenceValue = overlay;
            }
        }

        /// <summary>
        /// A health bar in the gold frame: the dark red empty bar and the green fill (resized by
        /// HudController) sit in the frame's recess, with the HP count over them.
        /// </summary>
        static (RectTransform fill, Text text) HealthBar(RectTransform panel, float anchorX, float y)
        {
            const float width = 440f, height = width * 150f / 1040f;  // the frame art's shape
            var bar = NewRect(panel, "Health");
            bar.anchorMin = bar.anchorMax = bar.pivot = new Vector2(anchorX, 1f);
            bar.anchoredPosition = new Vector2(0f, y);
            bar.sizeDelta = new Vector2(width, height);

            // The frame's transparent recess, in its pixels: x 74..966 of 1040, y 52..98 of 150 from the top.
            var recess = NewRect(bar, "Recess");
            Inset(recess, new Vector2(74f / 1040f, 1f - 98f / 150f), new Vector2(966f / 1040f, 1f - 52f / 150f));
            var empty = NewRect(recess, "Empty");
            Inset(empty, Vector2.zero, Vector2.one);
            Picture(empty, Art("Frames/FillShade.png"), EmptyColour);
            var fill = NewRect(recess, "Fill");
            Inset(fill, Vector2.zero, Vector2.one);
            fill.pivot = new Vector2(0f, 0.5f);
            Picture(fill, Art("Frames/FillShade.png"), HealthColour);
            var count = NewRect(recess, "Text");
            Inset(count, Vector2.zero, Vector2.one);
            var text = AddText(count, "100 / 100", 18, TextAnchor.MiddleCenter, Color.white);
            text.gameObject.AddComponent<Outline>().effectColor = new Color(0f, 0f, 0f, 0.85f);

            var frame = NewRect(bar, "Frame");
            Inset(frame, Vector2.zero, Vector2.one);
            Picture(frame, Art("Frames/HealthFrame.png"), Color.white);
            return (fill, text);
        }

        /// <summary>A non-interactive image (so it never swallows a tap meant for placing the arena).</summary>
        static Image Picture(RectTransform rect, Sprite sprite, Color colour)
        {
            var image = rect.gameObject.AddComponent<Image>();
            image.sprite = sprite;
            image.color = colour;
            image.raycastTarget = false;
            return image;
        }

        static void Inset(RectTransform rect, Vector2 anchorMin, Vector2 anchorMax)
        {
            rect.anchorMin = anchorMin;
            rect.anchorMax = anchorMax;
            rect.offsetMin = rect.offsetMax = Vector2.zero;
        }

        /// <summary>Loads a HUD texture as a UI sprite, setting its import options (and 9-slice border) once.</summary>
        static Sprite Art(string path, float border = 0f)
        {
            string full = $"{ArtDir}/{path}";
            var importer = AssetImporter.GetAtPath(full) as TextureImporter
                ?? throw new System.InvalidOperationException($"Missing HUD art {full}: run Tools/make_hud_art.py.");
            var wanted = new Vector4(border, border, border, border);
            if (importer.textureType != TextureImporterType.Sprite || importer.spriteBorder != wanted || importer.mipmapEnabled)
            {
                importer.textureType = TextureImporterType.Sprite;
                importer.spriteImportMode = SpriteImportMode.Single;
                importer.alphaIsTransparency = true;
                importer.mipmapEnabled = false;
                importer.spriteBorder = wanted;
                importer.SaveAndReimport();
            }
            return AssetDatabase.LoadAssetAtPath<Sprite>(full);
        }

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
            var button = FramedButton(panel, name, label, 24);
            var rect = (RectTransform)button.transform;
            rect.anchorMin = new Vector2(0f, 0f);
            rect.anchorMax = new Vector2(1f, 0f);
            rect.pivot = new Vector2(0.5f, 0f);
            rect.anchoredPosition = new Vector2(0f, y);
            rect.sizeDelta = new Vector2(0f, 76f);
            return button;
        }

        /// <summary>A button in the gold ButtonFrame with a centred gold label; the caller sizes and places it.</summary>
        static Button FramedButton(RectTransform parent, string name, string label, int fontSize)
        {
            var rect = NewRect(parent, name);
            var frame = rect.gameObject.AddComponent<Image>();
            frame.sprite = Art("Frames/ButtonFrame.png", border: 24f);
            frame.type = Image.Type.Sliced;
            var button = rect.gameObject.AddComponent<Button>();
            button.targetGraphic = frame;

            var text = NewRect(rect, "Label");
            Inset(text, Vector2.zero, Vector2.one);
            AddText(text, label, fontSize, TextAnchor.MiddleCenter, Gold);
            return button;
        }

        /// <summary>Buttons need an EventSystem to receive taps; this one reads the new Input System.</summary>
        static void EnsureEventSystem()
        {
            if (Object.FindAnyObjectByType<EventSystem>() != null)
                return;
            new GameObject("EventSystem", typeof(EventSystem), typeof(InputSystemUIInputModule));
        }

        /// <summary>A column pinned near the top-left (anchorX 0) or top-right (anchorX 1) corner, TopPanelInset in.</summary>
        static RectTransform Panel(Transform parent, string name, float anchorX)
        {
            var rect = NewRect(parent, name);
            rect.anchorMin = rect.anchorMax = rect.pivot = new Vector2(anchorX, 1f);
            rect.anchoredPosition = new Vector2(anchorX == 0f ? TopPanelInset.x : -TopPanelInset.x, -TopPanelInset.y);
            rect.sizeDelta = new Vector2(PanelWidth, 240f);
            return rect;
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
            // A dark outline and drop shadow keep the text readable over a bright camera image.
            text.gameObject.AddComponent<Outline>().effectColor = new Color(0f, 0f, 0f, 0.8f);
            var shadow = text.gameObject.AddComponent<Shadow>();
            shadow.effectColor = new Color(0f, 0f, 0f, 0.6f);
            shadow.effectDistance = new Vector2(3f, -3f);
            return text;
        }

        static Text AddText(RectTransform rect, string content, int size, TextAnchor alignment, Color colour)
        {
            var text = rect.gameObject.AddComponent<Text>();
            text.font = HudFont;
            text.text = content;
            text.fontSize = size;
            text.fontStyle = FontStyle.Normal;  // the font file is already Cinzel Bold
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
