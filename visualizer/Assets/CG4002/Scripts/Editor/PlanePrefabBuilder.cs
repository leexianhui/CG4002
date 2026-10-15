using UnityEditor;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.XR.ARFoundation;

namespace CG4002.EditorTools
{
    /// <summary>
    /// The prefab ARPlaneManager creates for each surface ARKit detects: a faint translucent fill
    /// with a green outline, so you can see what has been found before tapping to place the dummy.
    /// ARPlaneMeshVisualizer reshapes the mesh and outline to the plane's boundary as it grows.
    /// </summary>
    static class PlanePrefabBuilder
    {
        const string PrefabPath = "Assets/CG4002/AR/DetectedPlane.prefab";
        const string FillMaterialPath = "Assets/CG4002/AR/M_PlaneFill.mat";
        const string OutlineMaterialPath = "Assets/CG4002/AR/M_TrackingDebug.mat";

        public static GameObject Build()
        {
            var plane = new GameObject("DetectedPlane");
            plane.AddComponent<ARPlane>();
            plane.AddComponent<MeshFilter>();
            plane.AddComponent<MeshRenderer>().sharedMaterial = CreateFillMaterial();

            var outline = plane.AddComponent<LineRenderer>();
            outline.sharedMaterial = AssetDatabase.LoadAssetAtPath<Material>(OutlineMaterialPath);
            outline.widthMultiplier = 0.008f;
            outline.loop = true;
            outline.useWorldSpace = false;  // boundary points are given in the plane's own space
            outline.shadowCastingMode = ShadowCastingMode.Off;

            plane.AddComponent<ARPlaneMeshVisualizer>();
            return AssetIO.SavePrefab(plane, PrefabPath);
        }

        static Material CreateFillMaterial()
        {
            // URP Unlit switched to transparent: these are the properties and keyword the
            // material Inspector sets when you pick Surface Type = Transparent.
            var material = new Material(Shader.Find("Universal Render Pipeline/Unlit"));
            material.SetColor("_BaseColor", new Color(1f, 1f, 1f, 0.12f));
            material.SetFloat("_Surface", 1f);
            material.SetFloat("_Blend", 0f);
            material.SetFloat("_SrcBlend", (float)BlendMode.SrcAlpha);
            material.SetFloat("_DstBlend", (float)BlendMode.OneMinusSrcAlpha);
            material.SetFloat("_ZWrite", 0f);
            material.SetOverrideTag("RenderType", "Transparent");
            material.EnableKeyword("_SURFACE_TYPE_TRANSPARENT");
            material.renderQueue = (int)RenderQueue.Transparent;
            return AssetIO.Save(material, FillMaterialPath);
        }
    }
}
