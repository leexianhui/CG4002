using System.Collections.Generic;
using System.Linq;
using CG4002.Combat;
using CG4002.Models;
using UnityEditor;
using UnityEditor.Animations;
using UnityEngine;
using C = CG4002.Models.AvatarContract;

namespace CG4002.EditorTools
{
    /// <summary>
    /// One base state machine shared by all weapons, plus an override controller per weapon.
    ///
    ///   Layer 0 "Body":  Idle ── Attack & AttackSlot==n ──▶ state named after slot n ──(clip ends)──▶ Idle
    ///                    one state per AttackSlot (Horizontal, Charged, Vertical, ...);
    ///                    attack transitions come from Any State, so a new attack interrupts
    ///   Layer 1 "Guard": masked to the left arm, always playing Guard; AvatarView fades its weight.
    /// </summary>
    static class AnimatorFactory
    {
        const string AnimationDir = AssetIO.Root + "/Animation";

        public static AvatarMask CreateGuardMask(GameObject avatarRoot)
        {
            var mask = new AvatarMask();
            mask.AddTransformPath(avatarRoot.transform, true);
            for (int i = 0; i < mask.transformCount; i++)
            {
                string path = mask.GetTransformPath(i);
                bool active = MannequinRig.GuardBones.Any(bone => path == bone || path.StartsWith(bone + "/"));
                mask.SetTransformActive(i, active);
            }
            return AssetIO.Save(mask, $"{AnimationDir}/Mask_GuardArm.mask");
        }

        public class BaseController
        {
            public AnimatorController controller;
            public AnimationClip idle, guard;
            public readonly Dictionary<AttackSlot, AnimationClip> attacks = new();
        }

        public static BaseController CreateBaseController(AvatarMask guardMask)
        {
            // Placeholder clips: they only exist so each weapon's override controller has something to replace.
            var result = new BaseController
            {
                idle = AssetIO.Save(new AnimationClip(), $"{AnimationDir}/Base/Base_Idle.anim"),
                guard = AssetIO.Save(new AnimationClip(), $"{AnimationDir}/Base/Base_Guard.anim"),
            };
            foreach (var slot in C.AttackSlots)
                result.attacks[slot] = AssetIO.Save(new AnimationClip(), $"{AnimationDir}/Base/Base_{slot}.anim");

            // State machines are sub-assets, so rebuild the controller rather than copy it in place.
            string path = $"{AnimationDir}/Avatar_Base.controller";
            AssetDatabase.DeleteAsset(path);
            var controller = AnimatorController.CreateAnimatorControllerAtPath(path);
            controller.AddParameter(C.AttackTrigger, AnimatorControllerParameterType.Trigger);
            controller.AddParameter(C.AttackSlotInt, AnimatorControllerParameterType.Int);

            var layers = controller.layers;
            layers[C.BodyLayer].name = "Body";
            controller.layers = layers;

            var body = controller.layers[C.BodyLayer].stateMachine;
            var idle = body.AddState(C.IdleState, new Vector3(300, 0));
            idle.motion = result.idle;
            body.defaultState = idle;

            for (int i = 0; i < C.AttackSlots.Length; i++)
            {
                var slot = C.AttackSlots[i];
                var attack = body.AddState(C.AttackState(slot), new Vector3(600, -240 + 80 * i));
                attack.motion = result.attacks[slot];
                AddAttackTransition(body, attack, slot);
                AddReturnTransition(attack, idle);
            }

            controller.AddLayer("Guard");
            layers = controller.layers;
            layers[C.GuardLayer].defaultWeight = 0f;
            layers[C.GuardLayer].avatarMask = guardMask;
            layers[C.GuardLayer].blendingMode = AnimatorLayerBlendingMode.Override;
            controller.layers = layers;

            var guardMachine = controller.layers[C.GuardLayer].stateMachine;
            var guard = guardMachine.AddState(C.GuardState, new Vector3(300, 0));
            guard.motion = result.guard;
            guardMachine.defaultState = guard;

            result.controller = controller;
            return result;
        }

        static void AddAttackTransition(AnimatorStateMachine machine, AnimatorState target, AttackSlot slot)
        {
            var transition = machine.AddAnyStateTransition(target);
            transition.AddCondition(AnimatorConditionMode.If, 0f, C.AttackTrigger);
            transition.AddCondition(AnimatorConditionMode.Equals, (int)slot, C.AttackSlotInt);
            transition.hasExitTime = false;
            transition.hasFixedDuration = true;
            transition.duration = 0.06f;
            transition.canTransitionToSelf = true;  // a repeat attack restarts rather than being ignored
        }

        static void AddReturnTransition(AnimatorState from, AnimatorState to)
        {
            var transition = from.AddTransition(to);
            transition.hasExitTime = true;
            transition.exitTime = 0.9f;
            transition.hasFixedDuration = true;
            transition.duration = 0.12f;
        }

        public static AnimatorOverrideController CreateOverride(BaseController baseController, WeaponType weapon,
            AnimationClip idle, AnimationClip guard, IReadOnlyDictionary<AttackSlot, AnimationClip> attacks)
        {
            var overrides = new AnimatorOverrideController(baseController.controller);
            var pairs = new List<KeyValuePair<AnimationClip, AnimationClip>>
            {
                new(baseController.idle, idle),
                new(baseController.guard, guard),
            };
            foreach (var slot in C.AttackSlots)
                pairs.Add(new(baseController.attacks[slot], attacks[slot]));
            overrides.ApplyOverrides(pairs);
            return AssetIO.Save(overrides, $"{AnimationDir}/{weapon}/Avatar_{weapon}.overrideController");
        }

        public static AnimationClip SaveClip(AnimationClip clip, WeaponType weapon) =>
            AssetIO.Save(clip, $"{AnimationDir}/{weapon}/{clip.name}.anim");
    }

    static class DefinitionFactory
    {
        const string DefinitionDir = AssetIO.Root + "/Definitions";

        public static WeaponDefinition Create(WeaponType type, string displayName, WeaponView mainHand, ShieldView offHand,
            AnimatorOverrideController animations, Color accent, params (ActionType action, AttackSlot slot)[] moves)
        {
            var definition = ScriptableObject.CreateInstance<WeaponDefinition>();
            definition.type = type;
            definition.displayName = displayName;
            definition.mainHandPrefab = mainHand;
            definition.offHandPrefab = offHand;
            definition.animations = animations;
            definition.accentColor = accent;
            definition.moves = moves.Select(m => new WeaponDefinition.MoveBinding { action = m.action, slot = m.slot }).ToArray();
            return AssetIO.Save(definition, $"{DefinitionDir}/Weapon_{type}.asset");
        }

        public static WeaponCatalog CreateCatalog(params WeaponDefinition[] weapons)
        {
            var catalog = ScriptableObject.CreateInstance<WeaponCatalog>();
            catalog.weapons = weapons;
            return AssetIO.Save(catalog, $"{DefinitionDir}/WeaponCatalog.asset");
        }
    }
}
