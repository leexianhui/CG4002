using System.Collections.Generic;
using CG4002.Models;
using UnityEditor;
using UnityEngine;
using UnityEngine.Rendering;

namespace CG4002.EditorTools
{
    /// <summary>Generates the placeholder geometry: materials, weapon prefabs, the avatar and the impact burst.</summary>
    static class ModelFactory
    {
        const string MaterialDir = AssetIO.Root + "/Materials";
        const string MeshDir = AssetIO.Root + "/Meshes";
        const string PrefabDir = AssetIO.Root + "/Prefabs";

        // HDR accent colours. Emission at 1x this is the resting glow; the FX layer multiplies it.
        public static readonly Color SwordAccent = new Color(0.25f, 0.85f, 1f) * 1.5f;
        public static readonly Color SpearAccent = new Color(1f, 0.6f, 0.15f) * 1.5f;
        public static readonly Color MaceAccent = new Color(0.75f, 0.35f, 1f) * 1.5f;
        static readonly Color AvatarAccent = new Color(1f, 0.3f, 0.12f) * 2f;

        public class Materials
        {
            public Material avatarBody, avatarJoint, avatarAccent;
            public Material metal, leather, wood, shieldFace;
            public Material swordGlow, spearGlow, maceGlow;
            public Material ground;
            public Material fxTrail, fxParticle;
        }

        public static Materials CreateMaterials()
        {
            return new Materials
            {
                avatarBody = Lit("M_Avatar_Body", new Color(0.46f, 0.52f, 0.62f), 0.45f, 0.1f),
                avatarJoint = Lit("M_Avatar_Joint", new Color(0.13f, 0.14f, 0.17f), 0.3f, 0.2f),
                avatarAccent = Lit("M_Avatar_Accent", new Color(0.2f, 0.06f, 0.03f), 0.6f, 0f, AvatarAccent),
                metal = Lit("M_Metal", new Color(0.72f, 0.75f, 0.8f), 0.65f, 0.85f),
                leather = Lit("M_Leather", new Color(0.28f, 0.17f, 0.1f), 0.25f, 0f),
                wood = Lit("M_Wood", new Color(0.42f, 0.28f, 0.16f), 0.3f, 0f),
                shieldFace = Lit("M_ShieldFace", new Color(0.2f, 0.33f, 0.55f), 0.5f, 0.3f),
                swordGlow = Lit("M_Glow_Sword", new Color(0.05f, 0.15f, 0.2f), 0.7f, 0f, SwordAccent),
                spearGlow = Lit("M_Glow_Spear", new Color(0.2f, 0.12f, 0.03f), 0.7f, 0f, SpearAccent),
                maceGlow = Lit("M_Glow_Mace", new Color(0.15f, 0.06f, 0.2f), 0.7f, 0f, MaceAccent),
                ground = Lit("M_Showcase_Ground", new Color(0.2f, 0.21f, 0.24f), 0.2f, 0f),
                fxTrail = Fx("M_FX_Trail", Color.white, radial: false),
                fxParticle = Fx("M_FX_Particle", Color.white * 3f, radial: true),
            };
        }

        static Material Lit(string name, Color baseColor, float smoothness, float metallic, Color? emission = null)
        {
            var material = new Material(Shader.Find("Universal Render Pipeline/Lit"));
            material.SetColor("_BaseColor", baseColor);
            material.SetFloat("_Smoothness", smoothness);
            material.SetFloat("_Metallic", metallic);

            // Emission stays enabled on every material, black if unused, so the hit flash
            // and charge glow can drive it through a MaterialPropertyBlock.
            material.EnableKeyword("_EMISSION");
            material.SetColor("_EmissionColor", emission ?? Color.black);
            material.globalIlluminationFlags = MaterialGlobalIlluminationFlags.RealtimeEmissive;
            return AssetIO.Save(material, $"{MaterialDir}/{name}.mat");
        }

        static Material Fx(string name, Color tint, bool radial)
        {
            var material = new Material(Shader.Find("CG4002/FX Additive"));
            material.SetColor("_TintColor", tint);
            material.SetFloat("_Radial", radial ? 1f : 0f);
            material.renderQueue = (int)RenderQueue.Transparent;
            return AssetIO.Save(material, $"{MaterialDir}/{name}.mat");
        }

        // ---- Weapons ---------------------------------------------------------------------------
        // Weapon space: pivot at the grip centre, +Y toward the tip.

        public static WeaponView BuildSword(Materials m)
        {
            var root = new GameObject("Weapon_Sword");
            var view = root.AddComponent<WeaponView>();

            var body = new MeshBuilder()
                .Ico(0, new Vector3(0f, -0.115f, 0f), 0.028f)
                .Cylinder(1, new Vector3(0f, -0.09f, 0f), Quaternion.identity, 6, 0.017f, 0.015f, 0.18f)
                .Box(0, new Vector3(0f, 0.105f, 0f), new Vector3(0.2f, 0.03f, 0.04f))
                .Loft(0, Vector3.zero, Quaternion.identity, MeshBuilder.Diamond,
                    new Vector3(0.12f, 0.035f, 0.011f),
                    new Vector3(0.8f, 0.03f, 0.009f),
                    new Vector3(0.95f, 0f, 0f));
            AddMesh(root, "Body", body, "Sword_Body", m.metal, m.leather);

            var glow = new MeshBuilder()
                .Box(0, new Vector3(0f, 0.45f, 0f), new Vector3(0.01f, 0.58f, 0.024f))
                .Ico(0, new Vector3(0f, 0.105f, 0f), 0.024f);
            view.glowRenderers = new[] { AddMesh(root, "Glow", glow, "Sword_Glow", m.swordGlow) };

            view.trailBase = Anchor(root, "TrailBase", new Vector3(0f, 0.18f, 0f));
            view.trailTip = Anchor(root, "TrailTip", new Vector3(0f, 0.95f, 0f));
            view.strikePoint = Anchor(root, "StrikePoint", new Vector3(0f, 0.75f, 0f));
            return AssetIO.SavePrefab(root, $"{PrefabDir}/Weapon_Sword.prefab").GetComponent<WeaponView>();
        }

        /// <summary>Shield space: pivot at the forearm strap, +Z is the face, +Y toward the elbow.</summary>
        public static ShieldView BuildShield(Materials m)
        {
            var root = new GameObject("Weapon_Shield");
            var view = root.AddComponent<ShieldView>();
            Quaternion faceForward = Quaternion.Euler(90f, 0f, 0f);  // loft +Y becomes +Z
            var octagon = MeshBuilder.Polygon(8, 22.5f);

            var body = new MeshBuilder()
                .Loft(0, Vector3.zero, faceForward, octagon,
                    new Vector3(0f, 0.29f, 0.29f),
                    new Vector3(0.03f, 0.27f, 0.27f),
                    new Vector3(0.05f, 0.18f, 0.18f))
                .Ico(1, new Vector3(0f, 0f, 0.055f), 0.06f, new Vector3(1f, 1f, 0.7f))
                .Box(2, new Vector3(0f, 0f, -0.02f), new Vector3(0.05f, 0.14f, 0.03f));
            AddMesh(root, "Body", body, "Shield_Body", m.shieldFace, m.metal, m.leather);

            // Glowing rim: one bar per octagon edge, so the back of the shield doesn't glow.
            var rim = new MeshBuilder();
            float apothem = 0.3f * Mathf.Cos(22.5f * Mathf.Deg2Rad);
            float edge = 2f * 0.3f * Mathf.Sin(22.5f * Mathf.Deg2Rad);
            for (int i = 0; i < 8; i++)
            {
                float angle = i * 45f;
                Vector3 radial = Quaternion.Euler(0f, 0f, angle) * Vector3.right;
                rim.Box(0, radial * apothem + new Vector3(0f, 0f, 0.008f), new Vector3(edge + 0.012f, 0.022f, 0.026f),
                    Quaternion.Euler(0f, 0f, angle + 90f));
            }
            view.glowRenderers = new[] { AddMesh(root, "Glow", rim, "Shield_Glow", m.swordGlow) };

            view.blockFace = Anchor(root, "BlockFace", new Vector3(0f, 0f, 0.1f));
            return AssetIO.SavePrefab(root, $"{PrefabDir}/Weapon_Shield.prefab").GetComponent<ShieldView>();
        }

        public static WeaponView BuildSpear(Materials m)
        {
            var root = new GameObject("Weapon_Spear");
            var view = root.AddComponent<WeaponView>();

            var body = new MeshBuilder()
                .Cylinder(1, new Vector3(0f, -0.65f, 0f), Quaternion.identity, 6, 0.018f, 0.017f, 1.57f)
                .Ico(0, new Vector3(0f, -0.66f, 0f), 0.026f)
                .Cylinder(0, new Vector3(0f, 0.9f, 0f), Quaternion.identity, 6, 0.024f, 0.02f, 0.05f)
                .Loft(0, new Vector3(0f, 0.93f, 0f), Quaternion.identity, MeshBuilder.Diamond,
                    new Vector3(0f, 0.018f, 0.012f),
                    new Vector3(0.07f, 0.05f, 0.014f),
                    new Vector3(0.25f, 0f, 0f));
            AddMesh(root, "Body", body, "Spear_Body", m.metal, m.wood);

            var glow = new MeshBuilder()
                .Cylinder(0, new Vector3(0f, 0.87f, 0f), Quaternion.identity, 6, 0.026f, 0.026f, 0.025f)
                .Box(0, new Vector3(0f, 1.03f, 0f), new Vector3(0.008f, 0.14f, 0.03f))
                .Cylinder(0, new Vector3(0f, -0.1f, 0f), Quaternion.identity, 6, 0.021f, 0.021f, 0.03f);
            view.glowRenderers = new[] { AddMesh(root, "Glow", glow, "Spear_Glow", m.spearGlow) };

            view.trailBase = Anchor(root, "TrailBase", new Vector3(0f, 0.7f, 0f));
            view.trailTip = Anchor(root, "TrailTip", new Vector3(0f, 1.18f, 0f));
            view.strikePoint = Anchor(root, "StrikePoint", new Vector3(0f, 1.1f, 0f));
            return AssetIO.SavePrefab(root, $"{PrefabDir}/Weapon_Spear.prefab").GetComponent<WeaponView>();
        }

        public static WeaponView BuildMace(Materials m)
        {
            var root = new GameObject("Weapon_Mace");
            var view = root.AddComponent<WeaponView>();

            var body = new MeshBuilder()
                .Ico(0, new Vector3(0f, -0.13f, 0f), 0.03f)
                .Cylinder(1, new Vector3(0f, -0.11f, 0f), Quaternion.identity, 6, 0.02f, 0.018f, 0.55f)
                .Cylinder(0, new Vector3(0f, 0.42f, 0f), Quaternion.identity, 8, 0.05f, 0.065f, 0.2f)
                .Cylinder(0, new Vector3(0f, 0.62f, 0f), Quaternion.identity, 4, 0.03f, 0f, 0.08f);
            for (int i = 0; i < 6; i++)
            {
                float angle = i * 60f;
                Quaternion spin = Quaternion.Euler(0f, angle, 0f);
                body.Box(0, new Vector3(0f, 0.52f, 0f) + spin * new Vector3(0f, 0f, 0.085f), new Vector3(0.016f, 0.16f, 0.06f), spin);
            }
            AddMesh(root, "Body", body, "Mace_Body", m.metal, m.leather);

            var glow = new MeshBuilder()
                .Cylinder(0, new Vector3(0f, 0.49f, 0f), Quaternion.identity, 8, 0.07f, 0.07f, 0.04f)
                .Ico(0, new Vector3(0f, 0.7f, 0f), 0.02f);
            view.glowRenderers = new[] { AddMesh(root, "Glow", glow, "Mace_Glow", m.maceGlow) };

            view.trailBase = Anchor(root, "TrailBase", new Vector3(0f, 0.38f, 0f));
            view.trailTip = Anchor(root, "TrailTip", new Vector3(0f, 0.68f, 0f));
            view.strikePoint = Anchor(root, "StrikePoint", new Vector3(0f, 0.52f, 0f));
            return AssetIO.SavePrefab(root, $"{PrefabDir}/Weapon_Mace.prefab").GetComponent<WeaponView>();
        }

        // ---- Avatar ------------------------------------------------------------------------------

        /// <summary>
        /// Build the avatar hierarchy in the open scene. Not saved yet: the animation
        /// masks need the live hierarchy, and the Animator controller doesn't exist yet.
        /// </summary>
        public static AvatarView BuildAvatar(Materials m)
        {
            var root = new GameObject("OpponentAvatar");
            var animator = root.AddComponent<Animator>();
            animator.applyRootMotion = false;
            // AR opponents can leave the camera view; keep animating so events still fire.
            animator.cullingMode = AnimatorCullingMode.AlwaysAnimate;

            var bones = new Dictionary<string, Transform>();
            foreach (var (path, localPosition) in MannequinRig.Bones())
            {
                int slash = path.LastIndexOf('/');
                Transform parent = slash < 0 ? root.transform : bones[path.Substring(0, slash)];
                var bone = new GameObject(path.Substring(slash + 1)).transform;
                bone.SetParent(parent, false);
                bone.localPosition = localPosition;
                bones[path] = bone;
            }

            Material[] bodySet = { m.avatarBody, m.avatarJoint, m.avatarAccent };
            var renderers = new List<Renderer>();

            void Geo(string bonePath, string meshName, MeshBuilder builder)
            {
                renderers.Add(AddMesh(bones[bonePath].gameObject, bones[bonePath].name + "_Geo", builder, meshName, bodySet));
            }

            Quaternion down = Quaternion.Euler(180f, 0f, 0f);
            var octagon = MeshBuilder.Polygon(8, 22.5f);

            Geo(MannequinRig.Hips, "Avatar_Hips", new MeshBuilder()
                .Box(0, new Vector3(0f, -0.03f, 0f), new Vector3(0.3f, 0.14f, 0.18f))
                .Box(1, new Vector3(0f, 0.05f, 0f), new Vector3(0.31f, 0.04f, 0.19f)));

            Geo(MannequinRig.Spine, "Avatar_Spine", new MeshBuilder()
                .Cylinder(1, Vector3.zero, Quaternion.identity, 6, 0.12f, 0.13f, 0.18f));

            Geo(MannequinRig.Chest, "Avatar_Chest", new MeshBuilder()
                .Loft(0, Vector3.zero, Quaternion.identity, octagon,
                    new Vector3(0f, 0.15f, 0.1f),
                    new Vector3(0.18f, 0.21f, 0.13f),
                    new Vector3(0.3f, 0.16f, 0.1f))
                .Ico(2, new Vector3(0f, 0.16f, 0.125f), 0.035f, new Vector3(1f, 1f, 0.6f)));

            Geo(MannequinRig.Neck, "Avatar_Neck", new MeshBuilder()
                .Cylinder(1, Vector3.zero, Quaternion.identity, 6, 0.05f, 0.045f, 0.08f));

            Geo(MannequinRig.Head, "Avatar_Head", new MeshBuilder()
                .Ico(0, new Vector3(0f, 0.1f, 0f), 0.11f, new Vector3(0.9f, 1.05f, 1f))
                .Box(2, new Vector3(0f, 0.11f, 0.095f), new Vector3(0.15f, 0.03f, 0.04f)));

            var upperArm = new MeshBuilder()
                .Ico(1, Vector3.zero, 0.065f)
                .Cylinder(0, new Vector3(0f, -0.03f, 0f), down, 6, 0.05f, 0.042f, 0.23f);
            var forearm = new MeshBuilder()
                .Ico(1, Vector3.zero, 0.042f)
                .Cylinder(0, new Vector3(0f, -0.03f, 0f), down, 6, 0.042f, 0.034f, 0.2f)
                .Cylinder(2, new Vector3(0f, -0.2f, 0f), Quaternion.identity, 6, 0.038f, 0.038f, 0.03f);
            var hand = new MeshBuilder()
                .Box(1, new Vector3(0f, -0.045f, 0f), new Vector3(0.07f, 0.08f, 0.06f));
            var upperLeg = new MeshBuilder()
                .Ico(1, Vector3.zero, 0.06f)
                .Cylinder(0, new Vector3(0f, -0.03f, 0f), down, 6, 0.07f, 0.055f, 0.39f);
            var lowerLeg = new MeshBuilder()
                .Ico(1, Vector3.zero, 0.05f)
                .Cylinder(0, new Vector3(0f, -0.03f, 0f), down, 6, 0.052f, 0.04f, 0.37f);
            var foot = new MeshBuilder()
                .Box(1, new Vector3(0f, 0f, 0.05f), new Vector3(0.09f, 0.06f, 0.22f));

            // Limbs are symmetric about their bone axis, so left and right share mesh assets.
            Mesh upperArmMesh = SaveMesh(upperArm, "Avatar_UpperArm");
            Mesh forearmMesh = SaveMesh(forearm, "Avatar_Forearm");
            Mesh handMesh = SaveMesh(hand, "Avatar_Hand");
            Mesh upperLegMesh = SaveMesh(upperLeg, "Avatar_UpperLeg");
            Mesh lowerLegMesh = SaveMesh(lowerLeg, "Avatar_LowerLeg");
            Mesh footMesh = SaveMesh(foot, "Avatar_Foot");

            foreach (string s in new[] { "_R", "_L" })
            {
                string arm = $"{MannequinRig.Chest}/UpperArm{s}";
                string leg = $"{MannequinRig.Hips}/UpperLeg{s}";
                AddLimb(arm, upperArmMesh);
                AddLimb($"{arm}/Forearm{s}", forearmMesh);
                AddLimb($"{arm}/Forearm{s}/Hand{s}", handMesh);
                AddLimb(leg, upperLegMesh);
                AddLimb($"{leg}/LowerLeg{s}", lowerLegMesh);
                AddLimb($"{leg}/LowerLeg{s}/Foot{s}", footMesh);
            }

            void AddLimb(string bonePath, Mesh mesh)
            {
                renderers.Add(AddRenderer(bones[bonePath].gameObject, bones[bonePath].name + "_Geo", mesh, bodySet));
            }

            var view = root.AddComponent<AvatarView>();
            var serialized = new SerializedObject(view);
            serialized.FindProperty("animator").objectReferenceValue = animator;
            serialized.ApplyModifiedPropertiesWithoutUndo();

            // Grip: weapon +Y (toward the tip) points out of the front of the fist.
            view.mainHandSocket = Anchor(bones[MannequinRig.HandR].gameObject, "WeaponSocket_R",
                new Vector3(0f, -0.05f, 0f), Quaternion.Euler(90f, 0f, 0f));
            // Strap: shield face (+Z) sits on the front of the forearm.
            view.offHandSocket = Anchor(bones[MannequinRig.ForearmL].gameObject, "ShieldSocket_L",
                new Vector3(0f, -0.13f, 0.06f), Quaternion.identity);
            view.hitAnchor = Anchor(bones[MannequinRig.Chest].gameObject, "HitAnchor", new Vector3(0f, 0.14f, 0.16f));
            view.bodyRenderers = renderers.ToArray();
            return view;
        }

        public static AvatarView SaveAvatar(AvatarView avatar) =>
            AssetIO.SavePrefab(avatar.gameObject, $"{PrefabDir}/OpponentAvatar.prefab").GetComponent<AvatarView>();

        // ---- Effects -----------------------------------------------------------------------------

        public static ParticleSystem BuildImpactBurst(Materials m)
        {
            var root = new GameObject("FX_ImpactBurst");
            var sparks = root.AddComponent<ParticleSystem>();
            ConfigureBurst(sparks, m.fxParticle, count: 22, lifetime: new Vector2(0.15f, 0.35f),
                speed: new Vector2(1.5f, 4.5f), size: new Vector2(0.015f, 0.04f), stretched: true);

            var flash = new GameObject("Flash");
            flash.transform.SetParent(root.transform, false);
            var flashSystem = flash.AddComponent<ParticleSystem>();
            ConfigureBurst(flashSystem, m.fxParticle, count: 1, lifetime: new Vector2(0.08f, 0.08f),
                speed: Vector2.zero, size: new Vector2(0.45f, 0.45f), stretched: false);

            return AssetIO.SavePrefab(root, $"{PrefabDir}/FX_ImpactBurst.prefab").GetComponent<ParticleSystem>();
        }

        static void ConfigureBurst(ParticleSystem system, Material material, int count, Vector2 lifetime,
            Vector2 speed, Vector2 size, bool stretched)
        {
            system.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear);

            var main = system.main;
            main.duration = 0.5f;
            main.loop = false;
            main.playOnAwake = true;
            main.startLifetime = new ParticleSystem.MinMaxCurve(lifetime.x, lifetime.y);
            main.startSpeed = new ParticleSystem.MinMaxCurve(speed.x, speed.y);
            main.startSize = new ParticleSystem.MinMaxCurve(size.x, size.y);
            main.simulationSpace = ParticleSystemSimulationSpace.World;
            main.gravityModifier = stretched ? 0.6f : 0f;
            main.maxParticles = 64;
            main.stopAction = ParticleSystemStopAction.Destroy;

            var emission = system.emission;
            emission.rateOverTime = 0f;
            emission.SetBursts(new[] { new ParticleSystem.Burst(0f, (short)count) });

            var shape = system.shape;
            shape.enabled = stretched;
            shape.shapeType = ParticleSystemShapeType.Sphere;
            shape.radius = 0.04f;

            var colorOverLifetime = system.colorOverLifetime;
            colorOverLifetime.enabled = true;
            var gradient = new Gradient();
            gradient.SetKeys(
                new[] { new GradientColorKey(Color.white, 0f), new GradientColorKey(Color.white, 1f) },
                new[] { new GradientAlphaKey(1f, 0f), new GradientAlphaKey(0f, 1f) });
            colorOverLifetime.color = gradient;

            var sizeOverLifetime = system.sizeOverLifetime;
            sizeOverLifetime.enabled = true;
            sizeOverLifetime.size = new ParticleSystem.MinMaxCurve(1f, AnimationCurve.Linear(0f, 1f, 1f, 0.2f));

            var renderer = system.GetComponent<ParticleSystemRenderer>();
            renderer.sharedMaterial = material;
            renderer.renderMode = stretched ? ParticleSystemRenderMode.Stretch : ParticleSystemRenderMode.Billboard;
            renderer.velocityScale = 0.05f;
            renderer.lengthScale = 1.5f;
            renderer.shadowCastingMode = ShadowCastingMode.Off;
            renderer.receiveShadows = false;
        }

        // ---- Helpers -------------------------------------------------------------------------------

        static Mesh SaveMesh(MeshBuilder builder, string name) =>
            AssetIO.Save(builder.Build(name), $"{MeshDir}/{name}.asset");

        static Renderer AddMesh(GameObject parent, string childName, MeshBuilder builder, string meshName, params Material[] materials) =>
            AddRenderer(parent, childName, SaveMesh(builder, meshName), materials);

        static Renderer AddRenderer(GameObject parent, string childName, Mesh mesh, Material[] materials)
        {
            var child = new GameObject(childName);
            child.transform.SetParent(parent.transform, false);
            child.AddComponent<MeshFilter>().sharedMesh = mesh;
            var renderer = child.AddComponent<MeshRenderer>();

            // Only assign as many materials as the mesh has submeshes.
            var used = new Material[Mathf.Min(mesh.subMeshCount, materials.Length)];
            System.Array.Copy(materials, used, used.Length);
            renderer.sharedMaterials = used;
            renderer.shadowCastingMode = ShadowCastingMode.On;
            return renderer;
        }

        static Transform Anchor(GameObject parent, string name, Vector3 localPosition) =>
            Anchor(parent, name, localPosition, Quaternion.identity);

        static Transform Anchor(GameObject parent, string name, Vector3 localPosition, Quaternion localRotation)
        {
            var anchor = new GameObject(name).transform;
            anchor.SetParent(parent.transform, false);
            anchor.SetLocalPositionAndRotation(localPosition, localRotation);
            return anchor;
        }
    }
}
