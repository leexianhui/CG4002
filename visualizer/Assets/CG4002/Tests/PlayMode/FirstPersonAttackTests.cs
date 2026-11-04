#if UNITY_EDITOR
using System.Collections;
using System.Linq;
using CG4002.Combat;
using CG4002.Controllers;
using CG4002.Models;
using CG4002.Offline;
using CG4002.State;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;
using UnityEngine.TestTools;

namespace CG4002.Tests
{
    /// <summary>
    /// The local player's recognised attacks play first-person: only the weapon arm drawn, only
    /// for this player, never while on cooldown, hidden again once the move is over.
    /// Regenerate models before running (CG4002 > Generate Models).
    /// </summary>
    public class FirstPersonAttackTests
    {
        const string Generated = "Assets/CG4002/Generated";

        GameObject root;
        MessageDispatcher dispatcher;
        FirstPersonAttack view;

        [UnitySetUp]
        public IEnumerator SetUp()
        {
            root = new GameObject("Game");
            var gameState = root.AddComponent<GameState>();
            dispatcher = root.AddComponent<MessageDispatcher>();
            SetField(dispatcher, "gameState", gameState);
            var camera = new GameObject("Camera").AddComponent<Camera>();
            camera.transform.SetParent(root.transform, false);

            // Configure before Awake, which spawns the avatar under the camera.
            var holder = new GameObject("FirstPerson");
            holder.SetActive(false);
            holder.transform.SetParent(root.transform, false);
            view = holder.AddComponent<FirstPersonAttack>();
            SetField(view, "dispatcher", dispatcher);
            SetField(view, "gameState", gameState);
            SetField(view, "catalog", AssetDatabase.LoadAssetAtPath<WeaponCatalog>($"{Generated}/Definitions/WeaponCatalog.asset"));
            SetField(view, "avatarPrefab", AssetDatabase.LoadAssetAtPath<GameObject>($"{Generated}/Prefabs/OpponentAvatar.prefab").GetComponent<AvatarView>());
            SetField(view, "arCamera", camera);
            holder.SetActive(true);

            yield return Send(new WeaponChangedMessage { player = PlayerId.P1, weapon = WeaponType.SwordAndShield });
        }

        [UnityTearDown]
        public IEnumerator TearDown()
        {
            Object.Destroy(root);
            yield return null;
        }

        [UnityTest]
        public IEnumerator OwnAttack_PlaysWithOnlyTheWeaponArmDrawn_ThenHides()
        {
            yield return Send(new ActionRecognizedMessage { player = PlayerId.P1, action = ActionType.Thrust });
            Assert.That(view.IsPlaying, Is.True);
            Assert.That(view.Avatar.CurrentSlot, Is.EqualTo(AttackSlot.Thrust));
            Assert.That(view.Avatar.CurrentWeapon.GetComponentsInChildren<Renderer>().All(r => r.enabled), "weapon drawn");
            Assert.That(Drawn("Head_Geo"), Is.False, "head hidden");
            Assert.That(Drawn("Chest_Geo"), Is.False, "body hidden");
            Assert.That(Drawn("Forearm_R_Geo"), Is.True, "weapon forearm drawn");
            Assert.That(Drawn("Forearm_L_Geo"), Is.False, "off hand hidden");
            Assert.That(view.Avatar.CurrentShield.GetComponentsInChildren<Renderer>().Any(r => r.enabled), Is.False, "shield hidden");

            yield return new WaitForSeconds(1.2f);
            Assert.That(view.IsPlaying, Is.False);
            Assert.That(view.Avatar.GetComponentsInChildren<Renderer>().Any(r => r.enabled), Is.False, "hidden again");
        }

        [UnityTest]
        public IEnumerator OpponentAttack_OrOnCooldown_PlaysNothing()
        {
            yield return Send(new ActionRecognizedMessage { player = PlayerId.P2, action = ActionType.Thrust });
            Assert.That(view.IsPlaying, Is.False, "opponent's attack");

            yield return Send(new ActionRecognizedMessage { player = PlayerId.P1, action = ActionType.Thrust, onCooldown = true });
            Assert.That(view.IsPlaying, Is.False, "on cooldown");
        }

        [UnityTest]
        public IEnumerator OfflineEngine_ReportsEveryAttackAsRecognised()
        {
            var rules = root.AddComponent<LocalGameRules>();
            SetField(rules, "dispatcher", dispatcher);
            yield return null;  // rules.Start starts the match
            yield return null;

            rules.Attack(PlayerId.P1, ActionType.VerticalSwing);
            yield return null;
            Assert.That(view.IsPlaying, Is.True);
            Assert.That(view.Avatar.CurrentSlot, Is.EqualTo(AttackSlot.Vertical));

            ActionRecognizedMessage second = null;
            dispatcher.ActionRecognized += m => second = m;
            rules.Attack(PlayerId.P1, ActionType.VerticalSwing);  // within the cooldown
            yield return null;
            Assert.That(second, Is.Not.Null);
            Assert.That(second.onCooldown, Is.True);
        }

        bool Drawn(string part) =>
            view.Avatar.GetComponentsInChildren<Renderer>(true).First(r => r.name == part).enabled;

        IEnumerator Send(Message message)
        {
            dispatcher.Inbound.Enqueue(message);
            yield return null;
        }

        static void SetField(Object target, string name, object value) =>
            target.GetType()
                .GetField(name, System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)
                .SetValue(target, value);
    }
}
#endif
