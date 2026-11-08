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

        // Resolver step 11: a guard held longer than the parry window blocks; 25% of 10, rounded up.
        [UnityTest]
        public IEnumerator GuardingDefender_Blocks()
        {
            CombatOutcomeMessage outcome = null;
            dispatcher.OutcomeReceived += m => outcome = m;

            rules.SetGuard(PlayerId.P2, true);
            yield return new WaitForSeconds(0.25f);  // past match.yaml's 150 ms parry window
            rules.Attack(PlayerId.P1, ActionType.HorizontalSwing);
            yield return null;

            Assert.That(outcome.outcome, Is.EqualTo(Outcome.Block));
            Assert.That(outcome.damage, Is.EqualTo(3));
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(97));
            Assert.That(gameState.Get(PlayerId.P2).guarding, Is.True);
        }

        // Resolver step 10: a guard raised just before the hit parries: no damage, attacker staggered.
        [UnityTest]
        public IEnumerator GuardRaisedJustBeforeTheHit_Parries()
        {
            CombatOutcomeMessage outcome = null;
            dispatcher.OutcomeReceived += m => outcome = m;

            rules.SetGuard(PlayerId.P2, true);
            rules.Attack(PlayerId.P1, ActionType.HorizontalSwing);
            yield return null;

            Assert.That(outcome.outcome, Is.EqualTo(Outcome.Parry));
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(100));
            Assert.That(gameState.Get(PlayerId.P1).TotalFor(ActionType.HorizontalSwing), Is.EqualTo(2.4f + 0.5f).Within(1e-4f),
                "the move's own cooldown plus stagger_ms");
        }

        // Resolver step 8: further apart than the move's range_m (sword horizontal: 1.5 m) misses.
        [UnityTest]
        public IEnumerator BeyondTheMovesRange_Misses()
        {
            CombatOutcomeMessage outcome = null;
            dispatcher.OutcomeReceived += m => outcome = m;

            yield return ReportPosition(PlayerId.P1, new Vector3(0f, 1.5f, 2f), headingDegrees: 180f);
            rules.Attack(PlayerId.P1, ActionType.HorizontalSwing);
            yield return null;
            Assert.That(outcome.outcome, Is.EqualTo(Outcome.Miss));
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(100));
            Assert.That(gameState.Get(PlayerId.P1).RemainingFor(ActionType.HorizontalSwing), Is.GreaterThan(0f),
                "a miss still uses the move");

            Assert.That(rules.Attack(PlayerId.P1, ActionType.Thrust), Is.True, "thrust reaches 1.8 m: still short");
            yield return null;
            Assert.That(outcome.outcome, Is.EqualTo(Outcome.Miss));

            yield return ReportPosition(PlayerId.P1, new Vector3(0f, 1.5f, 1.2f), headingDegrees: 180f);
            rules.Attack(PlayerId.P1, ActionType.VerticalSwing);
            yield return null;
            Assert.That(outcome.outcome, Is.EqualTo(Outcome.Hit));
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(100 - 12));
        }

        // Resolver step 9: you only block what you face (guard arc 120 degrees). The enemy stands at
        // the origin; you're 1 m along +Z, so facing it is a heading of 180.
        [UnityTest]
        public IEnumerator GuardFacingAway_DoesNotBlock()
        {
            CombatOutcomeMessage outcome = null;
            dispatcher.OutcomeReceived += m => outcome = m;

            yield return ReportPosition(PlayerId.P1, new Vector3(0f, 1.5f, 1f), headingDegrees: 0f);
            rules.SetGuard(PlayerId.P1, true);
            yield return new WaitForSeconds(0.25f);
            rules.Attack(PlayerId.P2, ActionType.HorizontalSwing);
            yield return null;
            Assert.That(outcome.outcome, Is.EqualTo(Outcome.Hit), "facing away");

            yield return ReportPosition(PlayerId.P1, new Vector3(0f, 1.5f, 1f), headingDegrees: 180f - 50f);
            rules.Attack(PlayerId.P2, ActionType.Thrust);
            yield return null;
            Assert.That(outcome.outcome, Is.EqualTo(Outcome.Block), "facing it, within 60 degrees");
        }

        [Test]
        public void GameRules_ReadTheEnginesConfigFiles()
        {
            var parsed = GameRules.Parse(
                "weapons:\n  SWORD:      # balanced\n    moves:\n" +
                "      HORIZONTAL_SWING: { damage: 10, range_m: 1.5, cooldown_ms: 2400 }\n" +
                "  SPEAR:\n    moves:\n      THRUST:           { damage: 12, range_m: 2.5, cooldown_ms: 2100 }\n" +
                "  BOW:\n    moves:\n      THRUST: { damage: 1, range_m: 9, cooldown_ms: 1 }\n",
                "starting_hp: 120\nparry_window_ms: 200       # comment\nblock_multiplier: 0.5\nstagger_ms: 400\nguard_arc_deg: 90\n");

            Assert.That(parsed.TryGetMove(WeaponType.SwordAndShield, ActionType.HorizontalSwing, out var swing), Is.True);
            Assert.That((swing.damage, swing.range, swing.cooldown), Is.EqualTo((10, 1.5f, 2.4f)));
            Assert.That(parsed.TryGetMove(WeaponType.Spear, ActionType.Thrust, out var thrust), Is.True);
            Assert.That(thrust.range, Is.EqualTo(2.5f));
            Assert.That(parsed.TryGetMove(WeaponType.Spear, ActionType.HorizontalSwing, out _), Is.False);
            Assert.That((parsed.startingHp, parsed.parryWindow, parsed.blockMultiplier, parsed.stagger, parsed.guardArcDegrees),
                Is.EqualTo((120, 0.2f, 0.5f, 0.4f, 90f)));
        }

        [Test]
        public void ShippedRules_HaveEveryWeaponsSixMoves()
        {
            var shipped = GameRules.Load();
            foreach (var weapon in new[] { WeaponType.SwordAndShield, WeaponType.Spear, WeaponType.Mace })
            {
                foreach (var action in new[] { ActionType.HorizontalSwing, ActionType.VerticalSwing, ActionType.DiagonalSwing,
                             ActionType.Thrust, ActionType.SpinAttack, ActionType.Uppercut })
                    Assert.That(shipped.TryGetMove(weapon, action, out var move) && move.damage > 0 && move.range > 0f,
                        $"{weapon} {action}");
            }
            Assert.That(shipped.startingHp, Is.EqualTo(100));
        }

        IEnumerator ReportPosition(PlayerId player, Vector3 position, float headingDegrees)
        {
            dispatcher.Outbound.Enqueue(new PositionMessage { player = player, position = position, headingDegrees = headingDegrees });
            yield return null;  // the offline engine reads Outbound
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

        // Like the engine: each move only blocks itself, with weapons.yaml's durations.
        [UnityTest]
        public IEnumerator EachMove_CoolsDownOnItsOwn()
        {
            Assert.That(rules.Attack(PlayerId.P1, ActionType.HorizontalSwing), Is.True);
            Assert.That(rules.Attack(PlayerId.P1, ActionType.HorizontalSwing), Is.False, "same move, still cooling down");
            Assert.That(rules.Attack(PlayerId.P1, ActionType.Thrust), Is.True, "a different move is ready");
            yield return null;

            var self = gameState.Get(PlayerId.P1);
            Assert.That(self.TotalFor(ActionType.HorizontalSwing), Is.EqualTo(2.4f).Within(1e-4f));
            Assert.That(self.TotalFor(ActionType.Thrust), Is.EqualTo(1.8f).Within(1e-4f));
            Assert.That(self.RemainingFor(ActionType.HorizontalSwing), Is.GreaterThan(0f));
            Assert.That(self.RemainingFor(ActionType.VerticalSwing), Is.EqualTo(0f), "never used: ready");
        }

        [UnityTest]
        public IEnumerator MoveTheWeaponLacks_IsRecognisedButNotResolved()
        {
            Assert.That(rules.Attack(PlayerId.P1, ActionType.ChargedAttack), Is.False, "no weapon has a charged attack since 2026-11-06");
            yield return null;
            Assert.That(gameState.Get(PlayerId.P2).health, Is.EqualTo(100));
            Assert.That(rules.Attack(PlayerId.P1, ActionType.SpinAttack), Is.True, "every weapon has the six AI moves");
        }

        [UnityTest]
        public IEnumerator HealthReachingZero_EndsMatch()
        {
            SetField(rules, "cooldownScale", 0f);
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
