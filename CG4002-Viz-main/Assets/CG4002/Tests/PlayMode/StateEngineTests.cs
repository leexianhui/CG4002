#if UNITY_EDITOR
using System.Collections;
using CG4002.AR;
using CG4002.Combat;
using CG4002.Offline;
using CG4002.State;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

namespace CG4002.Tests
{
    /// <summary>
    /// The state engine end to end, without any avatar: the offline engine reads Outbound and
    /// answers on Inbound, the dispatcher applies it, GameState ends up with the right values.
    /// </summary>
    public class StateEngineTests
    {
        GameObject root;
        MessageDispatcher dispatcher;
        GameState gameState;
        LocalGameRules rules;

        [UnitySetUp]
        public IEnumerator SetUp()
        {
            root = new GameObject("Game");
            root.SetActive(false);  // wire references before Start runs
            gameState = root.AddComponent<GameState>();
            dispatcher = root.AddComponent<MessageDispatcher>();
            rules = root.AddComponent<LocalGameRules>();
            SetField(dispatcher, "gameState", gameState);
            SetField(rules, "dispatcher", dispatcher);
            root.SetActive(true);
            yield return null;  // Start: rules queue the match-start messages
            yield return null;  // dispatcher applies them
        }

        [UnityTearDown]
        public IEnumerator TearDown()
        {
            Object.Destroy(root);
            yield return null;
        }

        [UnityTest]
        public IEnumerator MatchStart_SetsRunningAndFullHealth()
        {
            Assert.That(gameState.Phase, Is.EqualTo(MatchPhase.Running));
            Assert.That(gameState.Opponent.health, Is.EqualTo(100));
            Assert.That(gameState.Opponent.weapon, Is.EqualTo(WeaponType.SwordAndShield));
            yield break;
        }

        [UnityTest]
        public IEnumerator WeaponMessage_IsAnnouncedEvenWhenUnchanged()
        {
            int announcements = 0;
            gameState.WeaponChanged += (_, _) => announcements++;
            dispatcher.Inbound.Enqueue(new WeaponChangedMessage { player = PlayerId.P2, weapon = WeaponType.SwordAndShield });
            yield return null;
            Assert.That(announcements, Is.EqualTo(1), "the avatar must be told to equip even the default weapon");
        }

        [UnityTest]
        public IEnumerator Hit_LowersDefenderHealth()
        {
            rules.Attack(PlayerId.P1, ActionType.HorizontalSwing);
            yield return null;
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(90));
            Assert.That(gameState.Get(PlayerId.P1).health, Is.EqualTo(100));
        }

        [UnityTest]
        public IEnumerator GuardingDefender_Blocks()
        {
            CombatOutcomeMessage outcome = null;
            dispatcher.OutcomeReceived += m => outcome = m;

            rules.SetGuard(PlayerId.P2, true);
            rules.Attack(PlayerId.P1, ActionType.HorizontalSwing);
            yield return null;

            Assert.That(outcome.outcome, Is.EqualTo(Outcome.Block));
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(100));
            Assert.That(gameState.Get(PlayerId.P2).guarding, Is.True);
        }

        [UnityTest]
        public IEnumerator SecondAttackDuringCooldown_IsIgnored()
        {
            Assert.That(rules.Attack(PlayerId.P1, ActionType.HorizontalSwing), Is.True);
            Assert.That(rules.Attack(PlayerId.P1, ActionType.HorizontalSwing), Is.False);
            yield return null;
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(90));
            Assert.That(gameState.Get(PlayerId.P1).CooldownRemaining, Is.GreaterThan(0f));
        }

        [UnityTest]
        public IEnumerator HealthReachingZero_EndsMatch()
        {
            SetField(rules, "attackCooldown", 0f);
            for (int i = 0; i < 10; i++)
                rules.Attack(PlayerId.P1, ActionType.HorizontalSwing);
            yield return null;

            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(0));
            Assert.That(gameState.Phase, Is.EqualTo(MatchPhase.Ended));
            Assert.That(gameState.Winner, Is.EqualTo(PlayerId.P1));
            Assert.That(rules.Attack(PlayerId.P1, ActionType.HorizontalSwing), Is.False, "no attacks after the match ends");
        }

        [UnityTest]
        public IEnumerator Outcome_IsAnnouncedAfterHealthIsApplied()
        {
            int healthSeenByListener = -1;
            dispatcher.OutcomeReceived += _ => healthSeenByListener = gameState.Get(PlayerId.P2).health;

            rules.Attack(PlayerId.P1, ActionType.HorizontalSwing);
            yield return null;

            Assert.That(healthSeenByListener, Is.EqualTo(90), "state must be updated before the event fires");
        }

        [UnityTest]
        public IEnumerator Dispatcher_DrainsWholeBurstInOneFrame()
        {
            for (int i = 0; i < 20; i++)
                dispatcher.Inbound.Enqueue(new HealthMessage { player = PlayerId.P2, health = 80 - i, maxHealth = 100 });
            yield return null;

            Assert.That(dispatcher.Inbound.Count, Is.EqualTo(0));
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(61));
        }

        [UnityTest]
        public IEnumerator UnknownSchemaVersion_IsIgnored()
        {
            LogAssert.Expect(LogType.Warning, new System.Text.RegularExpressions.Regex("schema version 99"));
            dispatcher.Inbound.Enqueue(new HealthMessage { player = PlayerId.P2, health = 5, maxHealth = 100, schemaVersion = 99 });
            yield return null;
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(100));
        }

        [UnityTest]
        public IEnumerator OutboundAction_IsResolvedByOfflineEngine()
        {
            CombatOutcomeMessage outcome = null;
            dispatcher.OutcomeReceived += m => outcome = m;

            dispatcher.Outbound.Enqueue(new ActionMessage { player = PlayerId.P1, action = ActionType.HorizontalSwing });
            yield return null;  // rules read Outbound, answer on Inbound
            yield return null;  // dispatcher applies the answer

            Assert.That(dispatcher.Outbound.Count, Is.EqualTo(0));
            Assert.That(outcome.outcome, Is.EqualTo(Outcome.Hit));
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(90));
        }

        [UnityTest]
        public IEnumerator OutboundStartMatchRequest_RestoresFullHealth()
        {
            rules.Attack(PlayerId.P1, ActionType.HorizontalSwing);
            yield return null;
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(90));

            dispatcher.Outbound.Enqueue(new StartMatchRequestMessage { player = PlayerId.P1 });
            yield return null;
            yield return null;
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(100));
        }

        [Test]
        public void PositionReporter_ConvertsToArenaCoordinates()
        {
            // Arena 2 m along world +X, turned 90 degrees so its +Z points along world +X.
            var arena = new GameObject("Arena").transform;
            arena.SetPositionAndRotation(new Vector3(2f, 0f, 0f), Quaternion.Euler(0f, 90f, 0f));

            // Standing 1 m in front of the origin (world x = 3), 1.5 m up, facing world -Z.
            var (position, heading) = PositionReporter.ToArena(arena, new Vector3(3f, 1.5f, 0f), Vector3.back);
            Object.DestroyImmediate(arena.gameObject);

            Assert.That(Vector3.Distance(position, new Vector3(0f, 1.5f, 1f)), Is.LessThan(1e-4f));
            Assert.That(heading, Is.EqualTo(90f).Within(1e-3f), "world -Z is the arena's +X: 90 degrees clockwise");
        }

        static void SetField(Object target, string name, object value) =>
            target.GetType()
                .GetField(name, System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)
                .SetValue(target, value);
    }
}
#endif
