using System;
using CG4002.AR;
using Unity.XR.CoreUtils;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEditor.XR.ARSubsystems;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Builds the reference image library for the prop markers and wires image tracking into the
    /// AR scene. Re-run after changing a marker size: CG4002 > Setup Image Tracking.
    /// </summary>
    public static class ImageTrackingSetup
    {
        const string ArDir = "Assets/CG4002/AR";
        const string LibraryPath = ArDir + "/PropMarkers.asset";
        const string DebugMaterialPath = ArDir + "/M_TrackingDebug.mat";
        const string ScenePath = "Assets/Scenes/SampleScene.unity";
        const string ImageDir = "Assets/UnityXRContent/Common/ReferenceImages/";

        // Printed width of each marker in metres. ARKit infers distance from this, so it must
        // match the ruler measurement of the print, not the size it was meant to print at.
        // Height is derived from the source image's aspect ratio.
        static readonly (string name, string file, float widthMetres)[] Markers =
        {
            ("Sword", "two.png", 0.09f),
            ("Shield", "Rafflesia.jpg", 0.15f),
            ("SwordBackup", "one.png", 0.09f),
        };

        // Moving images ARKit tracks every frame. Without this it treats images as fixed in the
        // room and the prop model would not follow your hand.
        const int MaxMovingImages = 2;

        [MenuItem("CG4002/Setup Image Tracking")]
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
            AssetIO.EnsureFolder(ArDir);
            var library = CreateLibrary();
            var material = CreateDebugMaterial();

            var scene = EditorSceneManager.OpenScene(ScenePath);
            var origin = UnityEngine.Object.FindAnyObjectByType<XROrigin>();
            if (origin == null)
                throw new InvalidOperationException($"No XR Origin in {ScenePath}.");

            var manager = GetOrAdd<ARTrackedImageManager>(origin.gameObject);
            var managerSettings = new SerializedObject(manager);
            managerSettings.FindProperty("m_SerializedLibrary").objectReferenceValue = library;
            managerSettings.FindProperty("m_MaxNumberOfMovingImages").intValue = MaxMovingImages;
            managerSettings.ApplyModifiedPropertiesWithoutUndo();

            var debug = GetOrAdd<TrackedImageDebug>(origin.gameObject);
            var debugSettings = new SerializedObject(debug);
            debugSettings.FindProperty("manager").objectReferenceValue = manager;
            debugSettings.FindProperty("arCamera").objectReferenceValue = origin.Camera;
            debugSettings.FindProperty("material").objectReferenceValue = material;
            debugSettings.ApplyModifiedPropertiesWithoutUndo();

            EditorSceneManager.MarkSceneDirty(scene);
            EditorSceneManager.SaveScene(scene);
            AssetDatabase.SaveAssets();
            Debug.Log($"[CG4002] Image tracking set up: {Markers.Length} markers, {MaxMovingImages} moving images.");
        }

        static XRReferenceImageLibrary CreateLibrary()
        {
            // Reference images are sub-entries of the library, so rebuild it rather than patch it.
            AssetDatabase.DeleteAsset(LibraryPath);
            var library = ScriptableObject.CreateInstance<XRReferenceImageLibrary>();
            AssetDatabase.CreateAsset(library, LibraryPath);

            foreach (var (name, file, width) in Markers)
            {
                string path = ImageDir + file;
                var importer = AssetImporter.GetAtPath(path) as TextureImporter;
                if (importer == null)
                    throw new InvalidOperationException($"Marker image not found: {path}");

                // By default Unity stretches imports to power-of-two sizes, which would distort the
                // reference relative to the print. Keep the image exactly as printed.
                if (importer.npotScale != TextureImporterNPOTScale.None)
                {
                    importer.npotScale = TextureImporterNPOTScale.None;
                    importer.mipmapEnabled = false;
                    importer.SaveAndReimport();
                }
                importer.GetSourceTextureWidthAndHeight(out int sourceWidth, out int sourceHeight);
                var texture = AssetDatabase.LoadAssetAtPath<Texture2D>(path);

                library.Add();
                int index = library.count - 1;
                library.SetName(index, name);
                library.SetTexture(index, texture, keepTexture: true);
                library.SetSpecifySize(index, true);
                library.SetSize(index, new Vector2(width, width * sourceHeight / sourceWidth));
            }
            EditorUtility.SetDirty(library);
            return library;
        }

        static Material CreateDebugMaterial()
        {
            var material = new Material(Shader.Find("Universal Render Pipeline/Unlit"));
            material.SetColor("_BaseColor", Color.green);
            return AssetIO.Save(material, DebugMaterialPath);
        }

        static T GetOrAdd<T>(GameObject target) where T : Component =>
            target.TryGetComponent(out T existing) ? existing : target.AddComponent<T>();
    }
}
