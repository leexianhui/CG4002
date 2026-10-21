#if UNITY_EDITOR
using System.Collections;
using System.Linq;
using CG4002.Combat;
using CG4002.Models;
using CG4002.Offline;
using CG4002.State;
using CG4002.UI;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;
using UnityEngine.TestTools;

namespace CG4002.Tests
{
    /// <summary>
    /// The selection screen follows the engine's weapon state: it opens on an explicit "no weapon",
    /// shows mirrored hologram dummies in front of the camera, and closes after the lock arrives.
    /// Regenerate models before running (CG4002 > Generate Models).
    /// </summary>
    public class WeaponSelectScreenTests
    {
        const string Generated = "Assets/CG4002/Generated";
        const float ConfirmSeconds = 0.2f;

        GameObject root;
        MessageDispatcher dispatcher;
        WeaponSelectScreen screen;
        Material hologram;

        [UnitySetUp]
        public IEnumerator SetUp()
        {
            root = new GameObject("Game");
            var gameState = root.AddComponent<GameState>();
            dispatcher = root.AddComponent<MessageDispatcher>();
            SetField(dispatcher, "gameState", gameState);

            var camera = new GameObject("Camera").AddComponent<Camera>();
            camera.transform.SetParent(root.transform, false);

            hologram = AssetDatabase.LoadAssetAtPath<Material>($"{Generated}/Materials/M_Hologram.mat");
            screen = new GameObject("WeaponSelect").AddComponent<WeaponSelectScreen>();
            screen.transform.SetParent(root.transform, false);
            SetField(screen, "gameState", gameState);
            SetField(screen, "catalog", AssetDatabase.LoadAssetAtPath<WeaponCatalog>($"{Generated}/Definitions/WeaponCatalog.asset"));
            SetField(screen, "dummyPrefab", AssetDatabase.LoadAssetAtPath<GameObject>($"{Generated}/Prefabs/OpponentAvatar.prefab").GetComponent<AvatarView>());
            SetField(screen, "hologram", hologram);
            SetField(screen, "arCamera", camera);
            SetField(screen, "confirmSeconds", ConfirmSeconds);
            Assert.That(hologram, Is.Not.Null, "Hologram material missing: run CG4002 > Generate Models.");
            yield return null;
        }

        [UnityTearDown]
        public IEnumerator TearDown()
        {
            Object.Destroy(root);
            var stage = GameObject.Find("Weapon Select Stage");
            if (stage != null)
                Object.Destroy(stage);
            yield return null;
        }

        [UnityTest]
        public IEnumerator StaysClosed_UntilTheEngineReportsNoWeapon()
        {
            yield return null;
            Assert.That(screen.IsSelecting, Is.False, "no snapshot yet");

            yield return Send(new WeaponChangedMessage { player = PlayerId.P1, weapon = WeaponType.SwordAndShield });
            Assert.That(screen.IsSelecting, Is.False, "weapon already locked");
        }

        [UnityTest]
        public IEnumerator NoWeapon_ShowsMirroredHologramDummyInFrontOfCamera()
        {
            yield return Send(new WeaponChangedMessage { player = PlayerId.P1, selected = false });

            Assert.That(screen.IsSelecting, Is.True);
            var stage = GameObject.Find("Weapon Select Stage");
            Assert.That(stage, Is.Not.Null);
            // The camera is at the origin looking along +Z: the dummies stand 2.5 m ahead, facing it.
            Assert.That(stage.transform.position.z, Is.EqualTo(2.5f).Within(1e-3f));
            Assert.That(Vector3.Dot(stage.transform.forward, Vector3.back), Is.GreaterThan(0.99f));

            var dummy = stage.GetComponentInChildren<AvatarView>();
            Assert.That(dummy.CurrentDefinition.type, Is.EqualTo(WeaponType.SwordAndShield));
            Assert.That(dummy.transform.localScale.x, Is.LessThan(0f), "mirrored");
            Assert.That(dummy.GetComponentsInChildren<Renderer>().SelectMany(r => r.sharedMaterials).All(m => m == hologram));
        }

        [UnityTest]
        public IEnumerator Lock_ConfirmsThenCloses()
        {
            yield return Send(new WeaponChangedMessage { player = PlayerId.P1, selected = false });
            yield return Send(new WeaponChangedMessage { player = PlayerId.P1, weapon = WeaponType.SwordAndShield });
            Assert.That(screen.IsSelecting, Is.True, "still showing the confirmation");

            yield return new WaitForSeconds(ConfirmSeconds + 0.1f);
            Assert.That(screen.IsSelecting, Is.False);
            yield return null;
            Assert.That(GameObject.Find("Weapon Select Stage"), Is.Null);
        }

        [UnityTest]
        public IEnumerator OfflineEngine_RequiringSelection_OpensUntilLockWeapon()
        {
            var rules = root.AddComponent<LocalGameRules>();
            SetField(rules, "dispatcher", dispatcher);
            SetField(rules, "requireWeaponSelection", true);
            yield return null;  // rules.Start sends the match start
            yield return null;  // the dispatcher applies it
            yield return null;  // the screen reacts
            Assert.That(screen.IsSelecting, Is.True);

            rules.LockWeapon(PlayerId.P1, WeaponType.SwordAndShield);
            yield return null;
            yield return new WaitForSeconds(ConfirmSeconds + 0.1f);
            Assert.That(screen.IsSelecting, Is.False);
        }

        IEnumerator Send(Message message)
        {
            dispatcher.Inbound.Enqueue(message);
            yield return null;  // dispatcher applies it
            yield return null;  // the screen reacts
        }

        static void SetField(Object target, string name, object value) =>
            target.GetType()
                .GetField(name, System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)
                .SetValue(target, value);
    }
}
#endif
