// Editor-only scene generator for the P1 comms demo HUD (visualizer_demo/README.md "Building the
// scene"). Builds the same GameObject graph the README's manual recipe describes, but through
// Unity's own Editor APIs so the resulting Assets/Scenes/Demo.unity is Unity-serialized (real
// GUIDs/file IDs) instead of hand-authored YAML. This is a scaffold, NOT the source of truth for
// the working phone scene/network setup. Refuses to overwrite an existing Demo.unity.
//
// Run via Window > CG4002 Demo > Create Demo Scene inside the Editor, or headless:
//   Unity.exe -batchmode -quit -projectPath "<project>" -executeMethod Cg4002.VisualizerDemo.EditorTools.CreateDemoScene.DoCreate

using Cg4002.VisualizerDemo.Controllers;
using Cg4002.VisualizerDemo.Net;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

namespace Cg4002.VisualizerDemo.EditorTools
{
    public static class CreateDemoScene
    {
        private const string ScenePath = "Assets/Scenes/Demo.unity";

        [MenuItem("Window/CG4002 Demo/Create Demo Scene")]
        public static void DoCreate()
        {
            if (System.IO.File.Exists(ScenePath))
                throw new System.InvalidOperationException("Demo.unity already exists. Preserve the working phone scene; use a separate project for scaffold generation.");
            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

            var player1Avatar = CreateAvatar("Player1Avatar", new Vector3(0f, 0.5f, 0f));
            var player2Avatar = CreateAvatar("Player2Avatar", new Vector3(1f, 0.5f, 0f));

            var canvasGo = CreateCanvas();
            var connectionStatusText = CreateLabel(canvasGo.transform, "ConnectionStatusText",
                "engine: disconnected", new Vector2(0f, 1f), new Vector2(0f, 1f), new Vector2(0f, 1f),
                new Vector2(20f, -20f), new Vector2(400f, 30f), TextAnchor.UpperLeft);

            var player1HpBar = CreateHpBar(canvasGo.transform, "Player1HpBar",
                new Vector2(0f, 1f), new Vector2(0f, 1f), new Vector2(0f, 1f), new Vector2(20f, -60f));
            var player1WeaponLabel = CreateLabel(canvasGo.transform, "Player1WeaponLabel", "P1: -",
                new Vector2(0f, 1f), new Vector2(0f, 1f), new Vector2(0f, 1f), new Vector2(20f, -90f),
                new Vector2(200f, 30f), TextAnchor.UpperLeft);

            var player2HpBar = CreateHpBar(canvasGo.transform, "Player2HpBar",
                new Vector2(1f, 1f), new Vector2(1f, 1f), new Vector2(1f, 1f), new Vector2(-20f, -60f));
            var player2WeaponLabel = CreateLabel(canvasGo.transform, "Player2WeaponLabel", "P2: -",
                new Vector2(1f, 1f), new Vector2(1f, 1f), new Vector2(1f, 1f), new Vector2(-20f, -90f),
                new Vector2(200f, 30f), TextAnchor.UpperRight);

            var combatFeedText = CreateLabel(canvasGo.transform, "CombatFeedText", "",
                new Vector2(0f, 0f), new Vector2(0f, 0f), new Vector2(0f, 0f), new Vector2(20f, 20f),
                new Vector2(500f, 160f), TextAnchor.LowerLeft);

            var engineStatusLabel = CreateLabel(canvasGo.transform, "EngineStatusLabel", "engine: -",
                new Vector2(0.5f, 1f), new Vector2(0.5f, 1f), new Vector2(0.5f, 1f), new Vector2(0f, -20f),
                new Vector2(400f, 30f), TextAnchor.UpperCenter);
            var player1PresenceLabel = CreateLabel(canvasGo.transform, "Player1PresenceLabel", "P1: -",
                new Vector2(0.5f, 1f), new Vector2(0.5f, 1f), new Vector2(0.5f, 1f), new Vector2(-110f, -50f),
                new Vector2(200f, 30f), TextAnchor.UpperCenter);
            var player2PresenceLabel = CreateLabel(canvasGo.transform, "Player2PresenceLabel", "P2: -",
                new Vector2(0.5f, 1f), new Vector2(0.5f, 1f), new Vector2(0.5f, 1f), new Vector2(110f, -50f),
                new Vector2(200f, 30f), TextAnchor.UpperCenter);

            var engineClientGo = new GameObject("EngineClient");
            var engineClient = engineClientGo.AddComponent<EngineClient>();
            engineClient.Host = "127.0.0.1";
            engineClient.Port = 8443;
            engineClient.PlayerId = 1;
            // BearerToken / PinnedCertSha256 deliberately left blank — secrets are never committed
            // to tracked source (visualizer_demo/README.md's setup step); fill them in per-machine
            // in the Inspector before running the demo.

            var hudControllerGo = new GameObject("HudController");
            var hud = hudControllerGo.AddComponent<DemoHudController>();
            hud.Client = engineClient;
            hud.Player1Avatar = player1Avatar.transform;
            hud.Player1HpBar = player1HpBar;
            hud.Player1WeaponLabel = player1WeaponLabel;
            hud.Player2Avatar = player2Avatar.transform;
            hud.Player2HpBar = player2HpBar;
            hud.Player2WeaponLabel = player2WeaponLabel;
            hud.CombatFeedText = combatFeedText;
            hud.ConnectionStatusText = connectionStatusText;
            hud.EngineStatusLabel = engineStatusLabel;
            hud.Player1PresenceLabel = player1PresenceLabel;
            hud.Player2PresenceLabel = player2PresenceLabel;

            var lightGo = new GameObject("Directional Light");
            var light = lightGo.AddComponent<Light>();
            light.type = LightType.Directional;
            lightGo.transform.rotation = Quaternion.Euler(50f, -30f, 0f);

            var cameraGo = new GameObject("Main Camera");
            cameraGo.tag = "MainCamera";
            var camera = cameraGo.AddComponent<Camera>();
            cameraGo.AddComponent<AudioListener>();
            cameraGo.transform.position = new Vector3(0.5f, 5f, -4f);
            cameraGo.transform.rotation = Quaternion.Euler(55f, 0f, 0f);
            camera.clearFlags = CameraClearFlags.Skybox;

            EnsureDirectoryExists("Assets/Scenes");
            bool saved = EditorSceneManager.SaveScene(scene, ScenePath);
            if (!saved)
            {
                throw new System.Exception($"CreateDemoScene: EditorSceneManager.SaveScene failed for '{ScenePath}'");
            }

            Debug.Log($"CreateDemoScene: saved '{ScenePath}' via Unity's own scene serializer.");

            EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(ScenePath, true) };

            ApplyPlayerSettings();
        }

        // U5: keeps ProjectSettings correct after a fresh clone + one batch-mode run, instead of
        // relying on someone clicking through Project Settings by hand. Idempotent - re-running
        // this just re-assigns the same values.
        private static void ApplyPlayerSettings()
        {
            PlayerSettings.companyName = "CG4002";
            PlayerSettings.productName = "CG4002 Visualizer Demo";
            PlayerSettings.SetApplicationIdentifier(NamedBuildTarget.Android, "com.cg4002.visualizerdemo");
            AssetDatabase.SaveAssets();
            Debug.Log("CreateDemoScene: applied companyName/productName/applicationIdentifier via PlayerSettings API.");
        }

        private static GameObject CreateAvatar(string name, Vector3 position)
        {
            var go = GameObject.CreatePrimitive(PrimitiveType.Capsule);
            go.name = name;
            go.transform.position = position;
            return go;
        }

        private static GameObject CreateCanvas()
        {
            var canvasGo = new GameObject("Canvas");
            var canvas = canvasGo.AddComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            var scaler = canvasGo.AddComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1080f, 1920f);
            canvasGo.AddComponent<GraphicRaycaster>();
            return canvasGo;
        }

        private static Text CreateLabel(
            Transform parent, string name, string initialText,
            Vector2 anchorMin, Vector2 anchorMax, Vector2 pivot,
            Vector2 anchoredPosition, Vector2 sizeDelta, TextAnchor alignment)
        {
            var go = new GameObject(name, typeof(RectTransform));
            go.transform.SetParent(parent, false);
            var rect = go.GetComponent<RectTransform>();
            rect.anchorMin = anchorMin;
            rect.anchorMax = anchorMax;
            rect.pivot = pivot;
            rect.anchoredPosition = anchoredPosition;
            rect.sizeDelta = sizeDelta;

            var text = go.AddComponent<Text>();
            text.text = initialText;
            text.font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
            text.fontSize = 24;
            text.alignment = alignment;
            text.color = Color.white;
            text.horizontalOverflow = HorizontalWrapMode.Wrap;
            text.verticalOverflow = VerticalWrapMode.Overflow;
            return text;
        }

        private static Slider CreateHpBar(
            Transform parent, string name,
            Vector2 anchorMin, Vector2 anchorMax, Vector2 pivot, Vector2 anchoredPosition)
        {
            var sliderGo = new GameObject(name, typeof(RectTransform));
            sliderGo.transform.SetParent(parent, false);
            var sliderRect = sliderGo.GetComponent<RectTransform>();
            sliderRect.anchorMin = anchorMin;
            sliderRect.anchorMax = anchorMax;
            sliderRect.pivot = pivot;
            sliderRect.anchoredPosition = anchoredPosition;
            sliderRect.sizeDelta = new Vector2(300f, 30f);

            var slider = sliderGo.AddComponent<Slider>();
            slider.interactable = false;
            slider.transition = Selectable.Transition.None;

            var backgroundGo = new GameObject("Background", typeof(RectTransform));
            backgroundGo.transform.SetParent(sliderGo.transform, false);
            var backgroundRect = backgroundGo.GetComponent<RectTransform>();
            backgroundRect.anchorMin = Vector2.zero;
            backgroundRect.anchorMax = Vector2.one;
            backgroundRect.sizeDelta = Vector2.zero;
            var backgroundImage = backgroundGo.AddComponent<Image>();
            backgroundImage.color = new Color(0.2f, 0.2f, 0.2f, 1f);

            var fillAreaGo = new GameObject("Fill Area", typeof(RectTransform));
            fillAreaGo.transform.SetParent(sliderGo.transform, false);
            var fillAreaRect = fillAreaGo.GetComponent<RectTransform>();
            fillAreaRect.anchorMin = new Vector2(0f, 0f);
            fillAreaRect.anchorMax = new Vector2(1f, 1f);
            fillAreaRect.sizeDelta = Vector2.zero;

            var fillGo = new GameObject("Fill", typeof(RectTransform));
            fillGo.transform.SetParent(fillAreaGo.transform, false);
            var fillRect = fillGo.GetComponent<RectTransform>();
            fillRect.anchorMin = Vector2.zero;
            fillRect.anchorMax = Vector2.one;
            fillRect.sizeDelta = Vector2.zero;
            var fillImage = fillGo.AddComponent<Image>();
            fillImage.color = new Color(0.2f, 0.8f, 0.2f, 1f);

            slider.fillRect = fillRect;
            slider.targetGraphic = fillImage;
            slider.direction = Slider.Direction.LeftToRight;
            slider.minValue = 0f;
            slider.maxValue = 100f;
            slider.value = 100f;
            slider.wholeNumbers = true;

            return slider;
        }

        private static void EnsureDirectoryExists(string assetsRelativeDir)
        {
            if (AssetDatabase.IsValidFolder(assetsRelativeDir)) return;
            var parent = System.IO.Path.GetDirectoryName(assetsRelativeDir).Replace('\\', '/');
            var leaf = System.IO.Path.GetFileName(assetsRelativeDir);
            if (!AssetDatabase.IsValidFolder(parent)) EnsureDirectoryExists(parent);
            AssetDatabase.CreateFolder(parent, leaf);
        }
    }
}
