using System.Linq;
using CG4002.FX;
using CG4002.Models;
using CG4002.Showcase;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.Universal;

namespace CG4002.EditorTools
{
    /// <summary>A plain 3D scene (no AR) for looking at models, animations and effects in the editor.</summary>
    static class ShowcaseSceneFactory
    {
        public const string ScenePath = "Assets/CG4002/Scenes/Showcase.unity";

        public static void Create(ModelFactory.Materials materials, AvatarView avatarPrefab, WeaponCatalog catalog, ParticleSystem impactBurst)
        {
            // Opening a scene unloads unused assets, which invalidates the references passed in,
            // so remember their paths and load them again afterwards.
            string avatarPath = AssetDatabase.GetAssetPath(avatarPrefab);
            string catalogPath = AssetDatabase.GetAssetPath(catalog);
            string burstPath = AssetDatabase.GetAssetPath(impactBurst);
            string groundMaterialPath = AssetDatabase.GetAssetPath(materials.ground);
            string trailMaterialPath = AssetDatabase.GetAssetPath(materials.fxTrail);

            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

            avatarPrefab = AssetDatabase.LoadAssetAtPath<AvatarView>(avatarPath);
            catalog = AssetDatabase.LoadAssetAtPath<WeaponCatalog>(catalogPath);
            impactBurst = AssetDatabase.LoadAssetAtPath<ParticleSystem>(burstPath);
            var groundMaterial = AssetDatabase.LoadAssetAtPath<Material>(groundMaterialPath);
            var trailMaterial = AssetDatabase.LoadAssetAtPath<Material>(trailMaterialPath);

            var light = new GameObject("Directional Light").AddComponent<Light>();
            light.type = LightType.Directional;
            light.intensity = 1.3f;
            light.shadows = LightShadows.Soft;
            light.transform.rotation = Quaternion.Euler(50f, -150f, 0f);

            RenderSettings.ambientMode = AmbientMode.Trilight;
            RenderSettings.ambientSkyColor = new Color(0.45f, 0.5f, 0.6f);
            RenderSettings.ambientEquatorColor = new Color(0.3f, 0.3f, 0.33f);
            RenderSettings.ambientGroundColor = new Color(0.12f, 0.12f, 0.14f);

            var camera = new GameObject("Main Camera", typeof(Camera), typeof(OrbitCamera)).GetComponent<Camera>();
            camera.tag = "MainCamera";
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = new Color(0.07f, 0.08f, 0.1f);
            camera.fieldOfView = 45f;
            camera.nearClipPlane = 0.05f;
            camera.GetUniversalAdditionalCameraData().renderPostProcessing = true;
            camera.GetComponent<OrbitCamera>().Apply();

            var volume = new GameObject("Global Volume").AddComponent<Volume>();
            volume.isGlobal = true;
            volume.sharedProfile = CreateVolumeProfile();

            var ground = new GameObject("Ground", typeof(MeshFilter), typeof(MeshRenderer));
            var groundMesh = AssetIO.Save(new MeshBuilder()
                .Cylinder(0, new Vector3(0f, -0.02f, 0f), Quaternion.identity, 24, 2.5f, 2.5f, 0.02f)
                .Build("Showcase_Ground"), $"{AssetIO.Root}/Meshes/Showcase_Ground.asset");
            ground.GetComponent<MeshFilter>().sharedMesh = groundMesh;
            ground.GetComponent<MeshRenderer>().sharedMaterial = groundMaterial;

            var avatar = (GameObject)PrefabUtility.InstantiatePrefab(avatarPrefab.gameObject);
            avatar.transform.SetPositionAndRotation(Vector3.zero, Quaternion.identity);

            var showcase = new GameObject("Showcase");
            var fx = showcase.AddComponent<CombatFxController>();
            var fxSerialized = new SerializedObject(fx);
            var avatars = fxSerialized.FindProperty("avatars");
            avatars.arraySize = 1;
            avatars.GetArrayElementAtIndex(0).objectReferenceValue = avatar.GetComponent<AvatarView>();
            fxSerialized.FindProperty("trailMaterial").objectReferenceValue = trailMaterial;
            fxSerialized.FindProperty("impactBurstPrefab").objectReferenceValue = impactBurst;
            fxSerialized.ApplyModifiedPropertiesWithoutUndo();

            var controller = showcase.AddComponent<ShowcaseController>();
            var controllerSerialized = new SerializedObject(controller);
            controllerSerialized.FindProperty("avatar").objectReferenceValue = avatar.GetComponent<AvatarView>();
            controllerSerialized.FindProperty("catalog").objectReferenceValue = catalog;
            controllerSerialized.FindProperty("fx").objectReferenceValue = fx;
            controllerSerialized.ApplyModifiedPropertiesWithoutUndo();

            AssetIO.EnsureFolder("Assets/CG4002/Scenes");
            EditorSceneManager.SaveScene(scene, ScenePath);
            AddToBuildSettings();
        }

        static VolumeProfile CreateVolumeProfile()
        {
            // Volume components are sub-assets of the profile, so recreate rather than copy in place.
            string path = $"{AssetIO.Root}/Showcase_Volume.asset";
            AssetDatabase.DeleteAsset(path);
            var profile = ScriptableObject.CreateInstance<VolumeProfile>();
            AssetDatabase.CreateAsset(profile, path);

            var bloom = profile.Add<Bloom>(true);
            bloom.threshold.Override(1f);
            bloom.intensity.Override(1.1f);
            bloom.scatter.Override(0.6f);
            bloom.name = "Bloom";
            AssetDatabase.AddObjectToAsset(bloom, profile);

            var tonemapping = profile.Add<Tonemapping>(true);
            tonemapping.mode.Override(TonemappingMode.Neutral);
            tonemapping.name = "Tonemapping";
            AssetDatabase.AddObjectToAsset(tonemapping, profile);
            return profile;
        }

        static void AddToBuildSettings()
        {
            var scenes = EditorBuildSettings.scenes.ToList();
            if (scenes.Any(s => s.path == ScenePath))
                return;
            scenes.Add(new EditorBuildSettingsScene(ScenePath, true));
            EditorBuildSettings.scenes = scenes.ToArray();
        }
    }
}
