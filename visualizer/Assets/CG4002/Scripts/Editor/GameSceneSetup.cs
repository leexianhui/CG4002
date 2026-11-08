using System;
using CG4002.AR;
using CG4002.Combat;
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
    ///   WeaponSelect   Canvas + WeaponSelectScreen (shown while the engine reports no weapon)
    ///   FirstPerson    Canvas + FirstPersonAttack (your recognised attacks, seen first-person)
    ///   Audio          GameAudio: sound effects and music (clips from Assets/CG4002/Audio)
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
            WireProps(propTracker, gameState);
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
            HudBuilder.BuildWeaponSelect();
            HudBuilder.BuildFirstPerson();
            BuildAudio();

            EditorSceneManager.MarkSceneDirty(scene);
            EditorSceneManager.SaveScene(scene);
            Debug.Log("[CG4002] Game scene set up.");
        }

        /// <summary>
        /// Which marker drives which weapon's model (and the shield's), and each prop's measurements. Measurements already
        /// set on the tracker are kept, so re-running doesn't undo values measured on the real props;
        /// the defaults below only fill in new entries. The spear and mace defaults are starting
        /// values (marker centred between the hands): re-measure on the real props.
        /// </summary>
        public static void WireProps(PropTracker tracker, GameState gameState)
        {
            if (tracker == null)
                throw new InvalidOperationException("No PropTracker in the scene: run CG4002 > Setup Game Scene first.");
            var props = new[]
            {
                (WeaponType.SwordAndShield, new[] { "Sword", "SwordBackup" }, "Prefabs/Weapon_Sword.prefab", 0.155f, 0.8f),
                // Card on the shaft halfway between the hands (40 cm apart); model's grip-to-tip 1.18 m.
                (WeaponType.Spear, new[] { "Spear" }, "Prefabs/Weapon_Spear.prefab", 0.2f, 0.98f),
                // Card on the handle just above the hands; model's grip-to-tip 0.68 m.
                (WeaponType.Mace, new[] { "Mace" }, "Prefabs/Weapon_Mace.prefab", 0.12f, 0.56f),
            };

            var settings = new SerializedObject(tracker);
            settings.FindProperty("gameState").objectReferenceValue = gameState;
            var list = settings.FindProperty("props");

            var measured = new System.Collections.Generic.Dictionary<int, (float grip, float tip)>();
            for (int i = 0; i < list.arraySize; i++)
            {
                var old = list.GetArrayElementAtIndex(i);
                float grip = old.FindPropertyRelative("markerToGripCentre").floatValue;
                float tip = old.FindPropertyRelative("markerToTip").floatValue;
                if (grip != 0f || tip != 0f)
                    measured[old.FindPropertyRelative("weapon").enumValueIndex] = (grip, tip);
            }

            list.arraySize = props.Length;
            for (int i = 0; i < props.Length; i++)
            {
                var (weapon, imageNames, prefab, markerToGrip, markerToTip) = props[i];
                var entry = list.GetArrayElementAtIndex(i);
                entry.FindPropertyRelative("weapon").enumValueIndex = (int)weapon;
                var names = entry.FindPropertyRelative("imageNames");
                names.arraySize = imageNames.Length;
                for (int n = 0; n < imageNames.Length; n++)
                    names.GetArrayElementAtIndex(n).stringValue = imageNames[n];
                entry.FindPropertyRelative("prefab").objectReferenceValue = Load<WeaponView>(prefab);
                if (measured.TryGetValue((int)weapon, out var kept))
                    (markerToGrip, markerToTip) = kept;
                entry.FindPropertyRelative("markerToGripCentre").floatValue = markerToGrip;
                entry.FindPropertyRelative("markerToTip").floatValue = markerToTip;
            }
            // The shield is tracked alongside the weapon; its size and offset are kept if already set.
            settings.FindProperty("shieldPrefab").objectReferenceValue = Load<ShieldView>("Prefabs/Weapon_Shield.prefab");
            settings.ApplyModifiedPropertiesWithoutUndo();
        }

        const string AudioDir = "Assets/CG4002/Audio";

        /// <summary>
        /// The Audio object: GameAudio wired to the open scene (dispatcher, state, enemy avatar, weapon
        /// selection) with every clip from Assets/CG4002/Audio. Rebuilt from scratch each time.
        /// </summary>
        public static void BuildAudio()
        {
            var existing = GameObject.Find("Audio");
            if (existing != null)
                UnityEngine.Object.DestroyImmediate(existing);

            var opponent = UnityEngine.Object.FindAnyObjectByType<AvatarController>(FindObjectsInactive.Include);
            var audio = new GameObject("Audio").AddComponent<GameAudio>();
            var settings = new SerializedObject(audio);
            settings.FindProperty("dispatcher").objectReferenceValue = UnityEngine.Object.FindAnyObjectByType<MessageDispatcher>();
            settings.FindProperty("gameState").objectReferenceValue = UnityEngine.Object.FindAnyObjectByType<GameState>();
            settings.FindProperty("opponent").objectReferenceValue = opponent != null ? opponent.GetComponent<AvatarView>() : null;
            settings.FindProperty("weaponSelect").objectReferenceValue = UnityEngine.Object.FindAnyObjectByType<CG4002.UI.WeaponSelectScreen>();

            // Lighter to heavier: the mace's swings are also pitched down.
            var swings = new[] { (WeaponType.SwordAndShield, "Sword", 1.08f), (WeaponType.Spear, "Spear", 1f), (WeaponType.Mace, "Mace", 0.85f) };
            var swingList = settings.FindProperty("swings");
            swingList.arraySize = swings.Length;
            for (int i = 0; i < swings.Length; i++)
            {
                var (weapon, name, pitch) = swings[i];
                var entry = swingList.GetArrayElementAtIndex(i);
                entry.FindPropertyRelative("weapon").enumValueIndex = (int)weapon;
                entry.FindPropertyRelative("pitch").floatValue = pitch;
                SetClips(entry.FindPropertyRelative("clips"), $"Swing_{name}_1.wav", $"Swing_{name}_2.wav", $"Swing_{name}_3.wav");
            }
            SetClips(settings.FindProperty("hits"), "Hit_1.ogg", "Hit_2.ogg", "Hit_3.ogg");
            SetClips(settings.FindProperty("shieldBlocks"), "Block_Shield_1.ogg", "Block_Shield_2.ogg");
            SetClips(settings.FindProperty("weaponBlocks"), "Block_Clash_1.ogg", "Block_Clash_2.ogg");
            foreach (var (field, file) in new[] { ("parry", "Parry.ogg"), ("miss", "Miss.wav"), ("guard", "Guard.ogg"),
                         ("equip", "Equip.wav"), ("victory", "Victory.wav"), ("defeat", "Defeat.wav") })
                settings.FindProperty(field).objectReferenceValue = Clip($"Sfx/{file}", music: false);
            settings.FindProperty("calmMusic").objectReferenceValue = Clip("Music/Calm_MinstrelGuild.mp3", music: true);
            settings.FindProperty("battleMusic").objectReferenceValue = Clip("Music/Battle_FiveArmies.mp3", music: true);
            settings.ApplyModifiedPropertiesWithoutUndo();
        }

        static void SetClips(SerializedProperty list, params string[] files)
        {
            list.arraySize = files.Length;
            for (int i = 0; i < files.Length; i++)
                list.GetArrayElementAtIndex(i).objectReferenceValue = Clip($"Sfx/{files[i]}", music: false);
        }

        /// <summary>
        /// Loads a clip, setting its import options once: music streams from storage (long, played
        /// once at a time); effects are decompressed on load so they start instantly.
        /// </summary>
        static AudioClip Clip(string path, bool music)
        {
            string full = $"{AudioDir}/{path}";
            var importer = AssetImporter.GetAtPath(full) as AudioImporter
                ?? throw new InvalidOperationException($"Missing audio {full}.");
            var wanted = importer.defaultSampleSettings;
            wanted.loadType = music ? AudioClipLoadType.Streaming : AudioClipLoadType.DecompressOnLoad;
            wanted.compressionFormat = AudioCompressionFormat.Vorbis;
            wanted.quality = music ? 0.5f : 0.7f;
            if (!importer.defaultSampleSettings.Equals(wanted))
            {
                importer.defaultSampleSettings = wanted;
                importer.SaveAndReimport();
            }
            return AssetDatabase.LoadAssetAtPath<AudioClip>(full);
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
