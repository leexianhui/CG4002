using System;
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

            var swordDefinition = DefinitionFactory.Create(WeaponType.SwordAndShield, "Sword & Shield", sword, shield, swordAnimations, ModelFactory.SwordAccent,
                (ActionType.HorizontalSwing, AttackSlot.Light),
                (ActionType.VerticalSwing, AttackSlot.Light),
                (ActionType.DiagonalSwing, AttackSlot.Light),
                (ActionType.Thrust, AttackSlot.Light),
                (ActionType.ChargedAttack, AttackSlot.Charged),
                (ActionType.SpinAttack, AttackSlot.Light));

            var spearDefinition = DefinitionFactory.Create(WeaponType.Spear, "Spear", spear, null, spearAnimations, ModelFactory.SpearAccent,
                (ActionType.Thrust, AttackSlot.Light),
                (ActionType.ChargedThrust, AttackSlot.Charged),
                (ActionType.Swing, AttackSlot.Light),
                (ActionType.OverheadStrike, AttackSlot.Light),
                (ActionType.BackStrike, AttackSlot.Light),
                (ActionType.SpinAttack, AttackSlot.Light));

            var maceDefinition = DefinitionFactory.Create(WeaponType.Mace, "Mace", mace, null, maceAnimations, ModelFactory.MaceAccent,
                (ActionType.VerticalSwing, AttackSlot.Light),
                (ActionType.DiagonalSwing, AttackSlot.Light),
                (ActionType.ChargedAttack, AttackSlot.Charged),
                (ActionType.HorizontalSwing, AttackSlot.Light),
                (ActionType.SpinAttack, AttackSlot.Light),
                (ActionType.Uppercut, AttackSlot.Light));

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

        static AnimatorOverrideController BakeWeapon(AnimatorFactory.BaseController baseController, WeaponType weapon)
        {
            var clips = AnimationLibrary.For(weapon);
            return AnimatorFactory.CreateOverride(baseController, weapon,
                AnimatorFactory.SaveClip(PoseSolver.Bake(clips.idle), weapon),
                AnimatorFactory.SaveClip(PoseSolver.Bake(clips.light), weapon),
                AnimatorFactory.SaveClip(PoseSolver.Bake(clips.charged), weapon),
                AnimatorFactory.SaveClip(PoseSolver.Bake(clips.guard), weapon));
        }
    }
}
