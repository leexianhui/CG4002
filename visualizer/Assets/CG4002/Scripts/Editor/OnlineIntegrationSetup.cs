using System;
using CG4002.AR;
using CG4002.Controllers;
using CG4002.Net;
using CG4002.State;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace CG4002.EditorTools
{
    /// <summary>Creates a separate online scene. Does not regenerate art or overwrite the offline scene.</summary>
    public static class OnlineIntegrationSetup
    {
        const string Source = "Assets/Scenes/SampleScene.unity";
        const string Destination = "Assets/Scenes/OnlineScene.unity";

        [MenuItem("CG4002/Integration/Create Online Scene")]
        public static void Create()
        {
            if (!EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;
            if (AssetDatabase.LoadAssetAtPath<SceneAsset>(Destination) != null)
            {
                EditorSceneManager.OpenScene(Destination);
                return; // Preserve all previously configured online values.
            }
            var scene = EditorSceneManager.OpenScene(Source);
            var dispatcher = UnityEngine.Object.FindAnyObjectByType<MessageDispatcher>();
            var state = UnityEngine.Object.FindAnyObjectByType<GameState>();
            var placer = UnityEngine.Object.FindAnyObjectByType<DummyPlacer>();
            var opponent = UnityEngine.Object.FindAnyObjectByType<AvatarController>(FindObjectsInactive.Include);
            var offline = GameObject.Find("OfflineEngine");
            if (dispatcher == null || state == null || placer == null || opponent == null || offline == null)
                throw new InvalidOperationException("Open the supplied complete offline scene before creating the online scene.");
            var placement = new SerializedObject(placer);
            var online = new GameObject("OnlineEngine");
            var calibration = online.AddComponent<SharedArenaCalibration>();
            calibration.placer = placer;
            calibration.arena = (Transform)placement.FindProperty("arena").objectReferenceValue;
            calibration.arCamera = (Camera)placement.FindProperty("arCamera").objectReferenceValue;
            var client = online.AddComponent<OnlineEngineClient>();
            client.dispatcher = dispatcher;
            client.gameState = state;
            client.opponent = opponent;
            client.calibration = calibration;
            client.weaponSelect = UnityEngine.Object.FindAnyObjectByType<CG4002.UI.WeaponSelectScreen>();
            client.offlineEngine = offline;
            offline.SetActive(false);
            if (!EditorSceneManager.SaveScene(scene, Destination))
                throw new InvalidOperationException("Could not save online scene");
            Selection.activeGameObject = online;
            Debug.Log("Online scene created. Add it to your build profile. Configure URL/player/token at runtime; calibrate both phones.");
        }

        /// <summary>
        /// Rebuilds the player-facing pieces in both scenes, touching nothing else: the HUD, the
        /// weapon selection screen, the first-person attack view, sound and music, demo mode's
        /// controls (OnlineScene), which prop each marker drives, and the bordered / split camera view.
        /// </summary>
        [MenuItem("CG4002/Integration/Refresh Player Views")]
        public static void RefreshPlayerViews()
        {
            if (!EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;
            RefreshPlayerViewsIn(Source);
            RefreshPlayerViewsIn(Destination);
        }

        public static void RefreshPlayerViewsBatch()
        {
            try
            {
                RefreshPlayerViewsIn(Source);
                RefreshPlayerViewsIn(Destination);
                EditorApplication.Exit(0);
            }
            catch (Exception e)
            {
                Debug.LogException(e);
                EditorApplication.Exit(1);
            }
        }

        static void RefreshPlayerViewsIn(string path)
        {
            if (AssetDatabase.LoadAssetAtPath<SceneAsset>(path) == null) return;
            var scene = EditorSceneManager.OpenScene(path);

            HudBuilder.Build(UnityEngine.Object.FindAnyObjectByType<GameState>(),
                UnityEngine.Object.FindAnyObjectByType<MessageDispatcher>(),
                UnityEngine.Object.FindAnyObjectByType<DummyPlacer>(),
                UnityEngine.Object.FindAnyObjectByType<TrackedImageDebug>());
            var screen = HudBuilder.BuildWeaponSelect();
            var client = UnityEngine.Object.FindAnyObjectByType<OnlineEngineClient>(FindObjectsInactive.Include);
            if (client != null)
            {
                client.weaponSelect = screen;
                // Demo mode (Docs/DemoMode.md): its control bar drives the scene's offline engine.
                var rules = client.offlineEngine != null ? client.offlineEngine.GetComponent<CG4002.Offline.LocalGameRules>() : null;
                client.demoMode = rules != null ? HudBuilder.BuildDemoControls(rules, screen) : null;
                EditorUtility.SetDirty(client);
            }
            HudBuilder.BuildFirstPerson();
            GameSceneSetup.BuildAudio();
            var origin = UnityEngine.Object.FindAnyObjectByType<Unity.XR.CoreUtils.XROrigin>();
            if (origin != null && !origin.Camera.TryGetComponent<BorderedView>(out _))
                origin.Camera.gameObject.AddComponent<BorderedView>();
            HudBuilder.WireSplitScreen();
            GameSceneSetup.WireProps(UnityEngine.Object.FindAnyObjectByType<PropTracker>(),
                UnityEngine.Object.FindAnyObjectByType<GameState>());

            EditorSceneManager.MarkSceneDirty(scene);
            EditorSceneManager.SaveScene(scene);
            Debug.Log($"[CG4002] Player views refreshed in {path}.");
        }
    }
}
