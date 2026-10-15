using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using CG4002.Models;
using UnityEditor;
using UnityEditor.Animations;
using UnityEngine;
using C = CG4002.Models.AvatarContract;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Checks that every model honours its contract. Most contract breaks fail silently at
    /// runtime (a trail that never appears, a glow on nothing), so they are caught here instead.
    /// Run it after replacing any model or clip: CG4002 > Validate Model Contracts.
    /// </summary>
    public static class ContractValidator
    {
        [MenuItem("CG4002/Validate Model Contracts")]
        public static void ValidateFromMenu()
        {
            var errors = Validate();
            if (errors.Count == 0)
                Debug.Log("[CG4002] All model contracts valid.");
            foreach (var error in errors)
                Debug.LogError($"[CG4002] {error}");
        }

        public static List<string> Validate()
        {
            var errors = new List<string>();

            var avatarReceivers = typeof(AvatarView).GetMethods(BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public)
                .Select(m => m.Name).ToHashSet();
            foreach (var name in new[] { C.ChargeStartEvent, C.StrikeOpenEvent, C.StrikeCloseEvent })
            {
                if (!avatarReceivers.Contains(name))
                    errors.Add($"AvatarView has no receiver method for animation event '{name}'.");
            }

            foreach (var guid in AssetDatabase.FindAssets("t:Prefab", new[] { "Assets/CG4002" }))
            {
                var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(AssetDatabase.GUIDToAssetPath(guid));
                if (prefab.TryGetComponent(out AvatarView avatar))
                    ValidateAvatar(avatar, errors);
            }

            var definitions = AssetDatabase.FindAssets("t:WeaponDefinition")
                .Select(g => AssetDatabase.LoadAssetAtPath<WeaponDefinition>(AssetDatabase.GUIDToAssetPath(g)));
            foreach (var definition in definitions)
                ValidateDefinition(definition, errors);

            return errors;
        }

        static void ValidateAvatar(AvatarView avatar, List<string> errors)
        {
            string who = $"Avatar '{avatar.name}'";
            if (avatar.Animator == null) errors.Add($"{who}: animator not assigned.");
            if (avatar.mainHandSocket == null) errors.Add($"{who}: mainHandSocket not assigned.");
            if (avatar.offHandSocket == null) errors.Add($"{who}: offHandSocket not assigned.");
            if (avatar.hitAnchor == null) errors.Add($"{who}: hitAnchor not assigned.");
            if (avatar.bodyRenderers == null || avatar.bodyRenderers.Length == 0 || avatar.bodyRenderers.Any(r => r == null))
                errors.Add($"{who}: bodyRenderers empty or contains a missing renderer.");
        }

        static void ValidateDefinition(WeaponDefinition definition, List<string> errors)
        {
            string who = $"Weapon '{definition.name}'";

            var weapon = definition.mainHandPrefab;
            if (weapon == null)
            {
                errors.Add($"{who}: mainHandPrefab not assigned.");
            }
            else
            {
                if (weapon.trailBase == null) errors.Add($"{who}: {weapon.name}.trailBase not assigned.");
                if (weapon.trailTip == null) errors.Add($"{who}: {weapon.name}.trailTip not assigned.");
                if (weapon.strikePoint == null) errors.Add($"{who}: {weapon.name}.strikePoint not assigned.");
                CheckGlow(weapon, who, errors);
            }

            if (definition.offHandPrefab != null)
            {
                if (definition.offHandPrefab.blockFace == null)
                    errors.Add($"{who}: {definition.offHandPrefab.name}.blockFace not assigned.");
                CheckGlow(definition.offHandPrefab, who, errors);
            }

            if (definition.moves == null || definition.moves.Length == 0)
                errors.Add($"{who}: move set is empty.");
            else if (definition.moves.GroupBy(m => m.action).Any(g => g.Count() > 1))
                errors.Add($"{who}: an action is bound more than once.");

            ValidateAnimations(definition, who, errors);
        }

        static void CheckGlow(HeldItemView item, string who, List<string> errors)
        {
            if (item.glowRenderers == null || item.glowRenderers.Length == 0 || item.glowRenderers.Any(r => r == null))
            {
                errors.Add($"{who}: {item.name}.glowRenderers empty or contains a missing renderer.");
                return;
            }
            foreach (var material in item.glowRenderers.SelectMany(r => r.sharedMaterials))
            {
                if (material != null && !material.IsKeywordEnabled("_EMISSION"))
                    errors.Add($"{who}: glow material '{material.name}' has emission disabled, so the charge glow won't show.");
            }
        }

        static void ValidateAnimations(WeaponDefinition definition, string who, List<string> errors)
        {
            var overrides = definition.animations;
            if (overrides == null)
            {
                errors.Add($"{who}: animations (override controller) not assigned.");
                return;
            }
            if (overrides.runtimeAnimatorController is not AnimatorController controller)
            {
                errors.Add($"{who}: override controller has no base controller.");
                return;
            }

            var parameters = controller.parameters.Select(p => p.name).ToHashSet();
            if (!parameters.Contains(C.AttackTrigger)) errors.Add($"{who}: base controller is missing parameter '{C.AttackTrigger}'.");
            if (!parameters.Contains(C.AttackSlotInt)) errors.Add($"{who}: base controller is missing parameter '{C.AttackSlotInt}'.");
            if (controller.layers.Length <= C.GuardLayer) errors.Add($"{who}: base controller has no guard layer.");

            var pairs = new List<KeyValuePair<AnimationClip, AnimationClip>>();
            overrides.GetOverrides(pairs);
            foreach (var pair in pairs.Where(p => p.Value == null))
                errors.Add($"{who}: no clip overrides '{pair.Key.name}'.");

            var clipsByState = new Dictionary<string, AnimationClip>();
            foreach (var layer in controller.layers)
            {
                foreach (var child in layer.stateMachine.states)
                {
                    if (child.state.motion is AnimationClip original)
                        clipsByState[child.state.name] = overrides[original];
                }
            }

            RequireEvents(clipsByState, C.LightState, who, errors, C.StrikeOpenEvent, C.StrikeCloseEvent);
            RequireEvents(clipsByState, C.ChargedState, who, errors, C.ChargeStartEvent, C.StrikeOpenEvent, C.StrikeCloseEvent);
        }

        static void RequireEvents(Dictionary<string, AnimationClip> clipsByState, string state, string who, List<string> errors, params string[] required)
        {
            if (!clipsByState.TryGetValue(state, out var clip) || clip == null)
            {
                errors.Add($"{who}: no clip for state '{state}'.");
                return;
            }
            var present = AnimationUtility.GetAnimationEvents(clip).Select(e => e.functionName).ToHashSet();
            foreach (var name in required.Where(n => !present.Contains(n)))
                errors.Add($"{who}: clip '{clip.name}' ({state}) is missing animation event '{name}'.");
        }
    }
}
