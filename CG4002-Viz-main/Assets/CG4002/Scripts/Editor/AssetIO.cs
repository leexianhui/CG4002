using System.IO;
using UnityEditor;
using UnityEngine;

namespace CG4002.EditorTools
{
    static class AssetIO
    {
        public const string Root = "Assets/CG4002/Generated";

        public static void EnsureFolder(string path)
        {
            if (AssetDatabase.IsValidFolder(path))
                return;
            string parent = Path.GetDirectoryName(path)!.Replace('\\', '/');
            EnsureFolder(parent);
            AssetDatabase.CreateFolder(parent, Path.GetFileName(path));
        }

        /// <summary>
        /// Save <paramref name="asset"/> at <paramref name="path"/>. If an asset of the same type is
        /// already there, its contents are overwritten in place so its GUID, and every reference
        /// to it from scenes or prefabs you made by hand, survive regeneration.
        /// </summary>
        public static T Save<T>(T asset, string path) where T : Object
        {
            EnsureFolder(Path.GetDirectoryName(path)!.Replace('\\', '/'));
            var existing = AssetDatabase.LoadAssetAtPath<T>(path);
            if (existing != null && existing.GetType() == asset.GetType())
            {
                EditorUtility.CopySerialized(asset, existing);
                existing.name = Path.GetFileNameWithoutExtension(path);
                EditorUtility.SetDirty(existing);
                Object.DestroyImmediate(asset);
                return existing;
            }
            if (existing != null)
                AssetDatabase.DeleteAsset(path);
            asset.name = Path.GetFileNameWithoutExtension(path);
            AssetDatabase.CreateAsset(asset, path);
            return asset;
        }

        public static GameObject SavePrefab(GameObject root, string path)
        {
            EnsureFolder(Path.GetDirectoryName(path)!.Replace('\\', '/'));
            var prefab = PrefabUtility.SaveAsPrefabAsset(root, path);
            Object.DestroyImmediate(root);
            return prefab;
        }
    }
}
