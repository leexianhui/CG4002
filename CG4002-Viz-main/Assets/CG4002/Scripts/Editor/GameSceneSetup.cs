using System;
using CG4002.AR;
using CG4002.Controllers;
using CG4002.FX;
using CG4002.Models;
using CG4002.Offline;
using CG4002.Showcase;
using CG4002.State;
using Unity.XR.CoreUtils;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.XR.ARFoundation;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Wires the state engine and the sword prop into the AR scene:
    ///
    ///   XR Origin      ARTrackedImageManager, TrackedImageDebug, PropTracker,
    ///                  ARPlaneManager, ARRaycastManager
    ///   Game           MessageDispatcher, GameState, DummyPlacer, PositionReporter,
    ///                  SwordTrail, CombatFxController
    ///   OfflineEngine  LocalGameRules, MockDataInjector, SwordHitDetector: the stand-in
    ///                  Game Engine. Disable it when a comms client is connected.
    ///   Arena          where the dummy stands (moved by DummyPlacer); arena coordinates are
    ///                  local positions under it
    ///     OpponentAvatar   AvatarView + AvatarController
    ///   HUD            Canvas + HudController + DebugPanel (layout from HudBuilder)
    ///
    /// Game, OfflineEngine and HUD are rebuilt from scratch each run. Run CG4002 > Setup Image
    /// Tracking first. Safe to re-run: CG4002 > Setup Game Scene.
    /// </summary>
    public static class GameSceneSetup
    {
        const string ScenePath = "Assets/Scenes/SampleScene.unity";

        [MenuItem("CG4002/Setup Game Scene")]
        public static void SetupFromMenu()
        {
            if (!EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo())
                return;
            Setup();
        }

        public static void SetupBatch()
        {
            try
            {
                Setup();
                EditorApplication.Exit(0);
            }
            catch (Exception e)
            {
                Debug.LogException(e);
                EditorApplication.Exit(1);
            }
        }

        public static void Setup()
        {
            var scene = EditorSceneManager.OpenScene(ScenePath);

            var avatar = UnityEngine.Object.FindAnyObjectByType<AvatarView>();
            if (avatar == null)
                throw new InvalidOperationException($"No OpponentAvatar in {ScenePath}.");

            var game = Recreate("Game");
            var dispatcher = game.AddComponent<MessageDispatcher>();
            var gameState = game.AddComponent<GameState>();
            var fx = game.AddComponent<CombatFxController>();

            var offline = Recreate("OfflineEngine");
            var rules = offline.AddComponent<LocalGameRules>();
            var mock = offline.AddComponent<MockDataInjector>();
            var hitDetector = offline.AddComponent<SwordHitDetector>();

            // The sword prop is tracked next to the image manager on the XR Origin.
            var origin = UnityEngine.Object.FindAnyObjectByType<XROrigin>();
            if (origin == null || !origin.TryGetComponent(out ARTrackedImageManager imageManager))
                throw new InvalidOperationException("No image tracking on the XR Origin: run CG4002 > Setup Image Tracking first.");
            var propTracker = GetOrAdd<PropTracker>(origin.gameObject);
            var trackingOverlay = origin.GetComponent<TrackedImageDebug>();

            // Floor detection for tap-to-place: planes found by ARKit, and raycasts against them.
            var planeManager = GetOrAdd<ARPlaneManager>(origin.gameObject);
            var planeSettings = new SerializedObject(planeManager);
            planeSettings.FindProperty("m_PlanePrefab").objectReferenceValue = PlanePrefabBuilder.Build();
            planeSettings.FindProperty("m_DetectionMode").intValue = (int)UnityEngine.XR.ARSubsystems.PlaneDetectionMode.Horizontal;
            planeSettings.ApplyModifiedPropertiesWithoutUndo();
            var raycastManager = GetOrAdd<ARRaycastManager>(origin.gameObject);

            var placer = game.AddComponent<DummyPlacer>();
            var positionReporter = game.AddComponent<PositionReporter>();
            var swordTrail = game.AddComponent<SwordTrail>();

            // Arena: created where the avatar currently stands, with the avatar at its origin.
            var arena = GameObject.Find("Arena");
            if (arena == null)
            {
                arena = new GameObject("Arena");
                arena.transform.SetPositionAndRotation(avatar.transform.position, avatar.transform.rotation);
            }
            avatar.transform.SetParent(arena.transform, false);
            avatar.transform.SetLocalPositionAndRotation(Vector3.zero, Quaternion.identity);

            // The avatar is now equipped by WEAPON_CHANGED messages, not at startup.
            if (avatar.TryGetComponent(out EquipOnStart equipOnStart))
                UnityEngine.Object.DestroyImmediate(equipOnStart, true);
            var controller = GetOrAdd<AvatarController>(avatar.gameObject);

            Set(dispatcher, "gameState", gameState);
            Set(rules, "dispatcher", dispatcher);
            Set(mock, "rules", rules);

            Set(propTracker, "manager", imageManager);
            Set(propTracker, "swordPrefab", Load<WeaponView>("Prefabs/Weapon_Sword.prefab"));
            Set(hitDetector, "prop", propTracker);
            Set(hitDetector, "dummy", avatar);
            Set(hitDetector, "dispatcher", dispatcher);
            Set(hitDetector, "gameState", gameState);
            Set(swordTrail, "prop", propTracker);
            Set(swordTrail, "trailMaterial", Load<Material>("Materials/M_FX_Trail.mat"));

            Set(placer, "raycastManager", raycastManager);
            Set(placer, "planeManager", planeManager);
            Set(placer, "arCamera", origin.Camera);
            Set(placer, "dispatcher", dispatcher);
            Set(placer, "gameState", gameState);
            Set(placer, "arena", arena.transform);
            Set(placer, "dummy", avatar.transform);

            Set(positionReporter, "arCamera", origin.Camera);
            Set(positionReporter, "arena", arena.transform);
            Set(positionReporter, "placer", placer);
            Set(positionReporter, "dispatcher", dispatcher);
            Set(positionReporter, "gameState", gameState);

            Set(controller, "avatar", avatar);
            Set(controller, "fx", fx);
            Set(controller, "catalog", Load<WeaponCatalog>("Definitions/WeaponCatalog.asset"));
            Set(controller, "gameState", gameState);
            Set(controller, "dispatcher", dispatcher);

            var fxSettings = new SerializedObject(fx);
            var avatars = fxSettings.FindProperty("avatars");
            avatars.arraySize = 1;
            avatars.GetArrayElementAtIndex(0).objectReferenceValue = avatar;
            fxSettings.FindProperty("trailMaterial").objectReferenceValue = Load<Material>("Materials/M_FX_Trail.mat");
            fxSettings.FindProperty("impactBurstPrefab").objectReferenceValue = Load<ParticleSystem>("Prefabs/FX_ImpactBurst.prefab");
            fxSettings.ApplyModifiedPropertiesWithoutUndo();

            HudBuilder.Build(gameState, dispatcher, placer, trackingOverlay);

            EditorSceneManager.MarkSceneDirty(scene);
            EditorSceneManager.SaveScene(scene);
            Debug.Log("[CG4002] Game scene set up.");
        }

        static T Load<T>(string path) where T : UnityEngine.Object =>
            AssetDatabase.LoadAssetAtPath<T>($"{AssetIO.Root}/{path}")
            ?? throw new InvalidOperationException($"Missing {AssetIO.Root}/{path}: run CG4002 > Generate Models.");

        static void Set(Component target, string field, UnityEngine.Object value)
        {
            var settings = new SerializedObject(target);
            settings.FindProperty(field).objectReferenceValue = value;
            settings.ApplyModifiedPropertiesWithoutUndo();
        }

        static GameObject Recreate(string name)
        {
            var existing = GameObject.Find(name);
            if (existing != null)
                UnityEngine.Object.DestroyImmediate(existing);
            return new GameObject(name);
        }

        static T GetOrAdd<T>(GameObject target) where T : Component =>
            target.TryGetComponent(out T existing) ? existing : target.AddComponent<T>();
    }
}
