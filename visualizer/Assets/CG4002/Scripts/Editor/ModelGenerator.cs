using System;
using System.Linq;
using CG4002.Combat;
using CG4002.Models;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Regenerates every placeholder model, animation, definition and the showcase scene.
    /// Assets are overwritten in place, so references to them elsewhere survive; hand edits
    /// to generated assets do not. Change the generator code instead, or copy an asset out
    /// of Generated/ before editing it.
    ///
    /// Batch mode: Unity -batchmode -quit -projectPath . -executeMethod CG4002.EditorTools.ModelGenerator.GenerateBatch
    /// </summary>
    public static class ModelGenerator
    {
        [MenuItem("CG4002/Generate Models")]
        public static void GenerateFromMenu()
        {
            if (!EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo())
                return;
            Generate();
            EditorSceneManager.OpenScene(ShowcaseSceneFactory.ScenePath);
        }

        public static void GenerateBatch()
        {
            try
            {
                Generate();
            }
            catch (Exception e)
            {
                Debug.LogException(e);
                EditorApplication.Exit(1);
                return;
            }
            EditorApplication.Exit(ContractValidator.Validate().Count == 0 ? 0 : 2);
        }

        public static void Generate()
        {
            // Build in a scratch scene so nothing leaks into whatever scene is open.
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

            var materials = ModelFactory.CreateMaterials();
            var sword = ModelFactory.BuildSword(materials);
            var shield = ModelFactory.BuildShield(materials);
            var spear = ModelFactory.BuildSpear(materials);
            var mace = ModelFactory.BuildMace(materials);
            var impactBurst = ModelFactory.BuildImpactBurst(materials);

            var avatar = ModelFactory.BuildAvatar(materials);
            var guardMask = AnimatorFactory.CreateGuardMask(avatar.gameObject);
            var baseController = AnimatorFactory.CreateBaseController(guardMask);

            var swordAnimations = BakeWeapon(baseController, WeaponType.SwordAndShield);
            var spearAnimations = BakeWeapon(baseController, WeaponType.Spear);
            var maceAnimations = BakeWeapon(baseController, WeaponType.Mace);

            // The AI classifies the same six attacks for every weapon (ai/config.py MOVE_CLASSES), so every
            // weapon animates all six; the engine's weapons.yaml decides which of them can land.
            var swordDefinition = DefinitionFactory.Create(WeaponType.SwordAndShield, "Sword & Shield", sword, shield, swordAnimations, ModelFactory.SwordAccent,
                ClassifierMoves((ActionType.ChargedAttack, AttackSlot.Charged)));

            var spearDefinition = DefinitionFactory.Create(WeaponType.Spear, "Spear", spear, null, spearAnimations, ModelFactory.SpearAccent,
                ClassifierMoves((ActionType.ChargedThrust, AttackSlot.Charged), (ActionType.Swing, AttackSlot.Horizontal)));

            var maceDefinition = DefinitionFactory.Create(WeaponType.Mace, "Mace", mace, null, maceAnimations, ModelFactory.MaceAccent,
                ClassifierMoves((ActionType.ChargedAttack, AttackSlot.Charged)));

            var catalog = DefinitionFactory.CreateCatalog(swordDefinition, spearDefinition, maceDefinition);

            // The prefab defaults to the sword set so it animates when dropped into a scene;
            // AvatarView.Equip replaces this at runtime.
            avatar.Animator.runtimeAnimatorController = swordAnimations;
            var avatarPrefab = ModelFactory.SaveAvatar(avatar);

            AssetDatabase.SaveAssets();
            ShowcaseSceneFactory.Create(materials, avatarPrefab, catalog, impactBurst);
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();

            var errors = ContractValidator.Validate();
            foreach (var error in errors)
                Debug.LogError($"[CG4002] {error}");
            Debug.Log(errors.Count == 0
                ? "[CG4002] Generation complete, all contracts valid."
                : $"[CG4002] Generation complete with {errors.Count} contract error(s).");
        }

        /// <summary>The six classifier attacks, each on its own slot, plus the weapon's engine-only moves.</summary>
        static (ActionType, AttackSlot)[] ClassifierMoves(params (ActionType, AttackSlot)[] extra) => new[]
        {
            (ActionType.HorizontalSwing, AttackSlot.Horizontal),
            (ActionType.VerticalSwing, AttackSlot.Vertical),
            (ActionType.DiagonalSwing, AttackSlot.Diagonal),
            (ActionType.Thrust, AttackSlot.Thrust),
            (ActionType.SpinAttack, AttackSlot.Spin),
            (ActionType.Uppercut, AttackSlot.Uppercut),
        }.Concat(extra).ToArray();

        static AnimatorOverrideController BakeWeapon(AnimatorFactory.BaseController baseController, WeaponType weapon)
        {
            var clips = AnimationLibrary.For(weapon);
            var attacks = clips.attacks.ToDictionary(pair => pair.Key, pair => AnimatorFactory.SaveClip(PoseSolver.Bake(pair.Value), weapon));
            return AnimatorFactory.CreateOverride(baseController, weapon,
                AnimatorFactory.SaveClip(PoseSolver.Bake(clips.idle), weapon),
                AnimatorFactory.SaveClip(PoseSolver.Bake(clips.guard), weapon),
                attacks);
        }
    }
}
