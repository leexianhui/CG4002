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

        /// <summary>Adds (or rebuilds) the weapon-selection screen in both scenes, touching nothing else.</summary>
        [MenuItem("CG4002/Integration/Add Weapon Select Screen")]
        public static void AddWeaponSelect()
        {
            if (!EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;
            AddWeaponSelectTo(Source);
            AddWeaponSelectTo(Destination);
        }

        public static void AddWeaponSelectBatch()
        {
            try
            {
                AddWeaponSelectTo(Source);
                AddWeaponSelectTo(Destination);
                EditorApplication.Exit(0);
            }
            catch (Exception e)
            {
                Debug.LogException(e);
                EditorApplication.Exit(1);
            }
        }

        static void AddWeaponSelectTo(string path)
        {
            if (AssetDatabase.LoadAssetAtPath<SceneAsset>(path) == null) return;
            var scene = EditorSceneManager.OpenScene(path);
            var screen = HudBuilder.BuildWeaponSelect();
            var client = UnityEngine.Object.FindAnyObjectByType<OnlineEngineClient>(FindObjectsInactive.Include);
            if (client != null)
            {
                client.weaponSelect = screen;
                EditorUtility.SetDirty(client);
            }
            EditorSceneManager.MarkSceneDirty(scene);
            EditorSceneManager.SaveScene(scene);
            Debug.Log($"[CG4002] Weapon select screen added to {path}.");
        }
    }
}
