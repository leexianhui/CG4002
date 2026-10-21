using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using CG4002.Combat;
using CG4002.FX;
using CG4002.Models;
using UnityEditor;
using UnityEditor.Animations;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.Universal;
using C = CG4002.Models.AvatarContract;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Renders contact sheets of each weapon's key poses (with trails and charge glow) without
    /// entering Play mode. Useful for checking a pose change, or for report screenshots.
    /// Each sheet: columns Idle, Guard, Horizontal wind-up, Horizontal strike, Charged hold, Charged strike;
    /// top row three-quarter front view, bottom row side view. A second sheet per weapon shows
    /// every attack as a film strip: one row per attack, from start to the end of its strike.
    /// </summary>
    public static class PreviewSheetRenderer
    {
        const int CellSize = 420;
        const int StripCell = 260;
        const int StripFrames = 7;
        static readonly int EmissionId = Shader.PropertyToID("_EmissionColor");

        [MenuItem("CG4002/Render Preview Sheets")]
        public static void RenderFromMenu()
        {
            string folder = EditorUtility.SaveFolderPanel("Save preview sheets", "", "");
            if (string.IsNullOrEmpty(folder) || !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo())
                return;
            Render(folder);
            EditorUtility.RevealInFinder(folder);
        }

        /// <summary>Batch entry: regenerate everything, then render sheets to the folder after -previewOut.</summary>
        public static void GenerateAndRenderBatch()
        {
            try
            {
                ModelGenerator.Generate();
                var args = Environment.GetCommandLineArgs();
                int index = Array.IndexOf(args, "-previewOut");
                Render(index >= 0 ? args[index + 1] : "Logs/Previews");
            }
            catch (Exception e)
            {
                Debug.LogException(e);
                EditorApplication.Exit(1);
                return;
            }
            EditorApplication.Exit(ContractValidator.Validate().Count == 0 ? 0 : 2);
        }

        public static void Render(string folder)
        {
            Directory.CreateDirectory(folder);

            // Load assets only after opening the scene: opening one unloads unused assets.
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            var catalog = AssetDatabase.LoadAssetAtPath<WeaponCatalog>($"{AssetIO.Root}/Definitions/WeaponCatalog.asset");
            var avatarPrefab = AssetDatabase.LoadAssetAtPath<GameObject>($"{AssetIO.Root}/Prefabs/OpponentAvatar.prefab");
            var trailMaterial = AssetDatabase.LoadAssetAtPath<Material>($"{AssetIO.Root}/Materials/M_FX_Trail.mat");
            var camera = BuildStage();

            // The first render after scene setup comes out overexposed (post-processing history
            // isn't warm yet), so render one throwaway frame.
            var warmUp = new Texture2D(CellSize, CellSize, TextureFormat.RGB24, false);
            CaptureInto(camera, warmUp, 0, 0);
            UnityEngine.Object.DestroyImmediate(warmUp);

            var avatarObject = (GameObject)PrefabUtility.InstantiatePrefab(avatarPrefab);
            var avatar = avatarObject.GetComponent<AvatarView>();
            var trail = new GameObject("Trail", typeof(MeshFilter), typeof(MeshRenderer), typeof(BladeTrail)).GetComponent<BladeTrail>();
            trail.GetComponent<MeshRenderer>().sharedMaterial = trailMaterial;

            foreach (var definition in catalog.weapons)
            {
                avatar.Equip(definition);
                var clips = ClipsByState(definition.animations);
                var horizontal = clips[C.AttackState(AttackSlot.Horizontal)];
                var charged = clips[C.AttackState(AttackSlot.Charged)];
                var sheet = new Texture2D(CellSize * 6, CellSize * 2, TextureFormat.RGB24, false);

                var poses = new List<Action>
                {
                    () => Pose(avatarObject, clips[C.IdleState], 0f),
                    () => { Pose(avatarObject, clips[C.IdleState], 0f); Pose(avatarObject, clips[C.GuardState], 0f); },
                    () => Pose(avatarObject, horizontal, EventTime(horizontal, C.StrikeOpenEvent) - 0.02f),
                    () => Strike(avatarObject, avatar, trail, horizontal, 0.12f, 1.2f),
                    () => { Pose(avatarObject, charged, EventTime(charged, C.StrikeOpenEvent) - 0.03f); SetGlow(avatar, 4f); },
                    () => { SetGlow(avatar, 4f); Strike(avatarObject, avatar, trail, charged, 0.28f, 2.2f); },
                };

                for (int column = 0; column < poses.Count; column++)
                {
                    trail.Clear();
                    SetGlow(avatar, 1f);
                    poses[column]();
                    Capture(camera, sheet, column, 1, yaw: 205f);
                    Capture(camera, sheet, column, 0, yaw: 270f);
                }

                Save(sheet, Path.Combine(folder, $"Preview_{definition.type}.png"));
                trail.Clear();
                SetGlow(avatar, 1f);
                RenderAttackStrips(camera, avatarObject, clips, Path.Combine(folder, $"Preview_{definition.type}_Attacks.png"));
            }

            UnityEngine.Object.DestroyImmediate(avatarObject);
            RenderModelLineup(camera, catalog, folder);
            Debug.Log($"[CG4002] Preview sheets written to {Path.GetFullPath(folder)}");
        }

        /// <summary>One row per attack slot, StripFrames poses from the clip's start to its StrikeClose.</summary>
        static void RenderAttackStrips(Camera camera, GameObject avatarObject, Dictionary<string, AnimationClip> clips, string path)
        {
            var slots = C.AttackSlots;
            var sheet = new Texture2D(StripCell * StripFrames, StripCell * slots.Length, TextureFormat.RGB24, false);
            var cell = new Texture2D(CellSize, CellSize, TextureFormat.RGB24, false);
            for (int row = 0; row < slots.Length; row++)
            {
                var clip = clips[C.AttackState(slots[row])];
                float end = EventTime(clip, C.StrikeCloseEvent);
                for (int column = 0; column < StripFrames; column++)
                {
                    Pose(avatarObject, clip, end * column / (StripFrames - 1));
                    Capture(camera, cell, 0, 0, yaw: 205f);
                    var scaled = Downscale(cell, StripCell);
                    // Rows run top to bottom in slot order; texture rows start at the bottom.
                    sheet.SetPixels(column * StripCell, (slots.Length - 1 - row) * StripCell, StripCell, StripCell, scaled);
                }
            }
            UnityEngine.Object.DestroyImmediate(cell);
            Save(sheet, path);
        }

        static Color[] Downscale(Texture2D source, int size)
        {
            var pixels = new Color[size * size];
            for (int y = 0; y < size; y++)
                for (int x = 0; x < size; x++)
                    pixels[y * size + x] = source.GetPixelBilinear((x + 0.5f) / size, (y + 0.5f) / size);
            return pixels;
        }

        static Camera BuildStage()
        {
            var light = new GameObject("Light").AddComponent<Light>();
            light.type = LightType.Directional;
            light.intensity = 1.3f;
            light.shadows = LightShadows.Soft;
            light.transform.rotation = Quaternion.Euler(50f, -150f, 0f);

            RenderSettings.ambientMode = AmbientMode.Trilight;
            RenderSettings.ambientSkyColor = new Color(0.45f, 0.5f, 0.6f);
            RenderSettings.ambientEquatorColor = new Color(0.3f, 0.3f, 0.33f);
            RenderSettings.ambientGroundColor = new Color(0.12f, 0.12f, 0.14f);

            var volume = new GameObject("Volume").AddComponent<Volume>();
            volume.isGlobal = true;
            volume.sharedProfile = AssetDatabase.LoadAssetAtPath<VolumeProfile>($"{AssetIO.Root}/Showcase_Volume.asset");

            var ground = new GameObject("Ground", typeof(MeshFilter), typeof(MeshRenderer));
            ground.GetComponent<MeshFilter>().sharedMesh = AssetDatabase.LoadAssetAtPath<Mesh>($"{AssetIO.Root}/Meshes/Showcase_Ground.asset");
            ground.GetComponent<MeshRenderer>().sharedMaterial = AssetDatabase.LoadAssetAtPath<Material>($"{AssetIO.Root}/Materials/M_Showcase_Ground.mat");

            var camera = new GameObject("Camera").AddComponent<Camera>();
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = new Color(0.07f, 0.08f, 0.1f);
            camera.fieldOfView = 45f;
            camera.nearClipPlane = 0.05f;
            camera.GetUniversalAdditionalCameraData().renderPostProcessing = true;
            return camera;
        }

        static void Pose(GameObject avatar, AnimationClip clip, float time) => clip.SampleAnimation(avatar, time);

        /// <summary>Pose at the middle of the strike window, with the trail built from samples since StrikeOpen.</summary>
        static void Strike(GameObject avatarObject, AvatarView avatar, BladeTrail trail, AnimationClip clip, float lifetime, float intensity)
        {
            float open = EventTime(clip, C.StrikeOpenEvent);
            float close = EventTime(clip, C.StrikeCloseEvent);
            float now = Mathf.Lerp(open, close, 0.55f);

            trail.Begin(null, null, avatar.CurrentDefinition.accentColor * intensity, lifetime);
            trail.End();
            for (float t = open; t <= now + 1e-4f; t += 1f / 60f)
            {
                Pose(avatarObject, clip, t);
                trail.AddSample(avatar.CurrentWeapon.trailBase.position, avatar.CurrentWeapon.trailTip.position, t);
            }
            Pose(avatarObject, clip, now);
            trail.Rebuild(now);
        }

        static void SetGlow(AvatarView avatar, float intensity)
        {
            var block = new MaterialPropertyBlock();
            block.SetColor(EmissionId, avatar.CurrentDefinition.accentColor * intensity);
            HeldItemView[] items = { avatar.CurrentWeapon, avatar.CurrentShield };
            foreach (var renderer in items.Where(i => i != null).SelectMany(i => i.glowRenderers))
                renderer.SetPropertyBlock(intensity == 1f ? null : block);
        }

        static void Capture(Camera camera, Texture2D sheet, int column, int row, float yaw)
        {
            var target = new Vector3(0f, 1.0f, 0.25f);
            Quaternion rotation = Quaternion.Euler(8f, yaw, 0f);
            camera.transform.SetPositionAndRotation(target - rotation * Vector3.forward * 3.9f, rotation);
            CaptureInto(camera, sheet, column, row);
        }

        static void CaptureInto(Camera camera, Texture2D sheet, int column, int row)
        {
            var rt = RenderTexture.GetTemporary(CellSize, CellSize, 24, RenderTextureFormat.ARGB32, RenderTextureReadWrite.sRGB, 4);
            camera.targetTexture = rt;
            camera.Render();
            var previous = RenderTexture.active;
            RenderTexture.active = rt;
            sheet.ReadPixels(new Rect(0, 0, CellSize, CellSize), column * CellSize, row * CellSize);
            RenderTexture.active = previous;
            camera.targetTexture = null;
            RenderTexture.ReleaseTemporary(rt);
        }

        static void RenderModelLineup(Camera camera, WeaponCatalog catalog, string folder)
        {
            var items = new List<GameObject>();
            void Place(Component prefab, Vector3 position, Quaternion rotation)
            {
                var instance = (GameObject)PrefabUtility.InstantiatePrefab(prefab.gameObject);
                instance.transform.SetPositionAndRotation(position, rotation);
                items.Add(instance);
            }

            var sword = catalog.weapons[0];
            Place(sword.mainHandPrefab, new Vector3(-0.9f, 0.25f, 0f), Quaternion.Euler(0f, 30f, 0f));
            Place(sword.offHandPrefab, new Vector3(-0.35f, 0.6f, 0f), Quaternion.Euler(0f, 160f, 0f));
            Place(catalog.weapons[1].mainHandPrefab, new Vector3(0.25f, 0.4f, 0f), Quaternion.Euler(0f, 30f, 0f));
            Place(catalog.weapons[2].mainHandPrefab, new Vector3(0.75f, 0.35f, 0f), Quaternion.Euler(0f, 30f, 0f));

            var sheet = new Texture2D(CellSize * 2, CellSize, TextureFormat.RGB24, false);
            var target = new Vector3(0f, 0.8f, 0f);
            foreach (var (column, yaw) in new[] { (0, 180f), (1, 225f) })
            {
                Quaternion rotation = Quaternion.Euler(10f, yaw, 0f);
                camera.transform.SetPositionAndRotation(target - rotation * Vector3.forward * 3.0f, rotation);
                CaptureInto(camera, sheet, column, 0);
            }
            Save(sheet, Path.Combine(folder, "Preview_Models.png"));
            foreach (var item in items)
                UnityEngine.Object.DestroyImmediate(item);
        }

        static Dictionary<string, AnimationClip> ClipsByState(AnimatorOverrideController overrides)
        {
            var controller = (AnimatorController)overrides.runtimeAnimatorController;
            return controller.layers
                .SelectMany(layer => layer.stateMachine.states)
                .ToDictionary(child => child.state.name, child => overrides[(AnimationClip)child.state.motion]);
        }

        static float EventTime(AnimationClip clip, string function) =>
            AnimationUtility.GetAnimationEvents(clip).First(e => e.functionName == function).time;

        static void Save(Texture2D sheet, string path)
        {
            sheet.Apply();
            File.WriteAllBytes(path, sheet.EncodeToPNG());
            UnityEngine.Object.DestroyImmediate(sheet);
        }
    }
}
