#if UNITY_EDITOR
using System.Collections;
using System.Reflection;
using CG4002.Models;
using CG4002.Showcase;
using NUnit.Framework;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace CG4002.Tests
{
    /// <summary>Loads the real showcase scene and runs the auto demo through every step. Any logged error fails the test.</summary>
    public class ShowcaseSceneTests
    {
        const string ScenePath = "Assets/CG4002/Scenes/Showcase.unity";

        [UnityTest]
        public IEnumerator AutoDemo_CyclesAllWeaponsWithoutErrors()
        {
            yield return EditorSceneManager.LoadSceneAsyncInPlayMode(ScenePath, new LoadSceneParameters(LoadSceneMode.Single));
            yield return null;

            var showcase = Object.FindAnyObjectByType<ShowcaseController>();
            var avatar = Object.FindAnyObjectByType<AvatarView>();
            Assert.That(showcase, Is.Not.Null);
            Assert.That(avatar.CurrentWeapon, Is.Not.Null, "Showcase should equip a weapon on start.");

            // Speed the demo up: 6 steps per weapon, run long enough to cycle through all three weapons.
            var flags = BindingFlags.Instance | BindingFlags.NonPublic;
            typeof(ShowcaseController).GetField("autoDemoStepSeconds", flags).SetValue(showcase, 0.2f);
            typeof(ShowcaseController).GetField("autoDemo", flags).SetValue(showcase, true);

            var seen = new System.Collections.Generic.HashSet<CG4002.Combat.WeaponType>();
            float end = Time.time + 20 * 0.2f;
            while (Time.time < end)
            {
                seen.Add(avatar.CurrentDefinition.type);
                yield return null;
            }

            Assert.That(seen.Count, Is.EqualTo(3), "Auto demo should reach every weapon.");
        }
    }
}
#endif
