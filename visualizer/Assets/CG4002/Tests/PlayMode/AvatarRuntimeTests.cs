#if UNITY_EDITOR
using System.Collections;
using System.Collections.Generic;
using CG4002.Combat;
using CG4002.FX;
using CG4002.Models;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;
using UnityEngine.TestTools;

namespace CG4002.Tests
{
    /// <summary>
    /// Runs the generated avatar in Play mode and checks the contract end to end:
    /// AvatarView calls drive the Animator, baked Animation Events come back as C# events,
    /// and the FX layer reacts. Regenerate models before running (CG4002 > Generate Models).
    /// </summary>
    public class AvatarRuntimeTests
    {
        const string Generated = "Assets/CG4002/Generated";

        WeaponCatalog catalog;
        AvatarView avatar;
        CombatFxController fx;
        readonly List<string> events = new();

        [UnitySetUp]
        public IEnumerator SetUp()
        {
            catalog = AssetDatabase.LoadAssetAtPath<WeaponCatalog>($"{Generated}/Definitions/WeaponCatalog.asset");
            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>($"{Generated}/Prefabs/OpponentAvatar.prefab");
            Assert.That(catalog, Is.Not.Null, "Weapon catalog missing: run CG4002 > Generate Models.");
            Assert.That(prefab, Is.Not.Null, "Avatar prefab missing: run CG4002 > Generate Models.");

            avatar = Object.Instantiate(prefab).GetComponent<AvatarView>();
            fx = new GameObject("FX").AddComponent<CombatFxController>();
            fx.Register(avatar);

            events.Clear();
            avatar.AttackStarted += (_, slot) => events.Add($"AttackStarted:{slot}");
            avatar.ChargeStarted += (_, _) => events.Add("ChargeStart");
            avatar.StrikeOpened += (_, _) => events.Add("StrikeOpen");
            avatar.StrikeClosed += (_, _) => events.Add("StrikeClose");
            yield return null;
        }

        [UnityTearDown]
        public IEnumerator TearDown()
        {
            Object.Destroy(fx.gameObject);
            Object.Destroy(avatar.gameObject);
            yield return null;
        }

        static IEnumerable<WeaponType> AllWeapons => new[] { WeaponType.SwordAndShield, WeaponType.Spear, WeaponType.Mace };

        [UnityTest]
        public IEnumerator Equip_AttachesModelsToSocketsAndSwapsAnimations([ValueSource(nameof(AllWeapons))] WeaponType weapon)
        {
            var definition = catalog.Get(weapon);
            avatar.Equip(definition);
            yield return null;

            Assert.That(avatar.CurrentWeapon.transform.parent, Is.EqualTo(avatar.mainHandSocket));
            Assert.That(avatar.Animator.runtimeAnimatorController, Is.EqualTo(definition.animations));
            if (definition.offHandPrefab != null)
                Assert.That(avatar.CurrentShield.transform.parent, Is.EqualTo(avatar.offHandSocket));
            else
                Assert.That(avatar.CurrentShield, Is.Null);
        }

        [UnityTest]
        public IEnumerator Equip_ReplacesPreviouslyHeldModels()
        {
            avatar.Equip(catalog.Get(WeaponType.SwordAndShield));
            yield return null;
            avatar.Equip(catalog.Get(WeaponType.Mace));
            yield return null;

            Assert.That(avatar.mainHandSocket.childCount, Is.EqualTo(1));
            Assert.That(avatar.offHandSocket.childCount, Is.EqualTo(0));
        }

        [UnityTest]
        public IEnumerator LightAttack_OpensAndClosesStrikeWindow([ValueSource(nameof(AllWeapons))] WeaponType weapon)
        {
            yield return EquipAndSettle(weapon);
            avatar.PlayAttack(AttackSlot.Light);
            yield return new WaitForSeconds(1f);

            CollectionAssert.AreEqual(new[] { "AttackStarted:Light", "StrikeOpen", "StrikeClose" }, events);
        }

        [UnityTest]
        public IEnumerator ChargedAttack_FiresChargeThenStrikeWindow([ValueSource(nameof(AllWeapons))] WeaponType weapon)
        {
            yield return EquipAndSettle(weapon);
            avatar.PlayAttack(AttackSlot.Charged);
            yield return new WaitForSeconds(1.3f);

            CollectionAssert.AreEqual(new[] { "AttackStarted:Charged", "ChargeStart", "StrikeOpen", "StrikeClose" }, events);
        }

        [UnityTest]
        public IEnumerator Attack_ReturnsToIdleAfterClipEnds()
        {
            yield return EquipAndSettle(WeaponType.SwordAndShield);
            avatar.PlayAttack(AttackSlot.Charged);
            yield return new WaitForSeconds(1.5f);

            Assert.That(avatar.Animator.GetCurrentAnimatorStateInfo(AvatarContract.BodyLayer).IsName(AvatarContract.IdleState));
        }

        [UnityTest]
        public IEnumerator NewAttack_InterruptsAndClosesOpenStrike()
        {
            yield return EquipAndSettle(WeaponType.SwordAndShield);
            avatar.PlayAttack(AttackSlot.Light);
            yield return WaitFor(() => events.Contains("StrikeOpen"), 1f);

            avatar.PlayAttack(AttackSlot.Charged);
            // The open strike must be closed before the new attack starts, so the trail never
            // carries over into the next swing's wind-up.
            int close = events.IndexOf("StrikeClose");
            int second = events.IndexOf("AttackStarted:Charged");
            Assert.That(close, Is.GreaterThanOrEqualTo(0));
            Assert.That(close, Is.LessThan(second));
        }

        [UnityTest]
        public IEnumerator Guard_BlendsGuardLayerInAndOut()
        {
            yield return EquipAndSettle(WeaponType.SwordAndShield);

            avatar.SetGuard(true);
            yield return new WaitForSeconds(0.4f);
            Assert.That(avatar.Animator.GetLayerWeight(AvatarContract.GuardLayer), Is.EqualTo(1f).Within(1e-3f));

            avatar.SetGuard(false);
            yield return new WaitForSeconds(0.4f);
            Assert.That(avatar.Animator.GetLayerWeight(AvatarContract.GuardLayer), Is.EqualTo(0f).Within(1e-3f));
        }

        [UnityTest]
        public IEnumerator StrikeWindow_BuildsTrailMesh()
        {
            yield return EquipAndSettle(WeaponType.SwordAndShield);
            var trail = GameObject.Find($"{avatar.name} Trail").GetComponent<MeshFilter>();

            avatar.PlayAttack(AttackSlot.Light);
            yield return WaitFor(() => events.Contains("StrikeOpen"), 1f);
            for (int i = 0; i < 4; i++)
                yield return null;

            Assert.That(trail.sharedMesh.vertexCount, Is.GreaterThan(0));
        }

        [UnityTest]
        public IEnumerator EveryWeapon_MapsChargedActionToChargedSlot()
        {
            var charged = new Dictionary<WeaponType, ActionType>
            {
                [WeaponType.SwordAndShield] = ActionType.ChargedAttack,
                [WeaponType.Spear] = ActionType.ChargedThrust,
                [WeaponType.Mace] = ActionType.ChargedAttack,
            };
            foreach (var (weapon, action) in charged)
            {
                Assert.That(catalog.Get(weapon).TryGetSlot(action, out var slot), Is.True, $"{weapon} lacks {action}");
                Assert.That(slot, Is.EqualTo(AttackSlot.Charged));
                Assert.That(catalog.Get(weapon).moves.Length, Is.EqualTo(6), $"{weapon} should list all six classifier moves");
            }
            yield break;
        }

        IEnumerator EquipAndSettle(WeaponType weapon)
        {
            avatar.Equip(catalog.Get(weapon));
            yield return new WaitForSeconds(0.1f);
            events.Clear();
        }

        static IEnumerator WaitFor(System.Func<bool> condition, float timeout)
        {
            float start = Time.time;
            while (!condition() && Time.time - start < timeout)
                yield return null;
        }
    }
}
#endif
