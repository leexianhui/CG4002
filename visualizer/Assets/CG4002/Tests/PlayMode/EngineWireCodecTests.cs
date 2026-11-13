using System;
using System.Linq;
using CG4002.Combat;
using CG4002.Net;
using CG4002.State;
using Newtonsoft.Json.Linq;
using NUnit.Framework;
using UnityEngine;

namespace CG4002.Tests
{
    public class EngineWireCodecTests
    {
        public const string Snapshot = @"{""type"":""STATE_SNAPSHOT"",""version"":1,""timestamp"":5000,
            ""phase"":""PAUSED"",""winnerId"":null,""reason"":""NOT_READY"",""players"":[
            {""playerId"":1,""hp"":90,""maxHp"":100,""weapon"":""SPEAR"",""guardState"":""IDLE"",
             ""connected"":true,""ready"":true,""position"":{""x"":2,""y"":3,""heading"":0},
             ""cooldownAction"":""THRUST"",""cooldowns"":[
               {""action"":""THRUST"",""remainingMs"":500,""totalMs"":3200}]},
            {""playerId"":2,""hp"":80,""maxHp"":100,""weapon"":null,""guardState"":""GUARDING"",
             ""connected"":true,""ready"":false,""position"":null,""cooldownAction"":null,""cooldowns"":[]}] }";

        [Test]
        public void ResetMatchIsExactlyTheOperatorCommandAndItsAnnouncementIsRecognised()
        {
            // The engine accepts only this exact object on /control (comms engine/app.py).
            Assert.That(JToken.DeepEquals(JObject.Parse(EngineWireCodec.ResetMatch()),
                JObject.Parse(@"{""type"":""RESET_MATCH"",""version"":1}")), Is.True);
            const string reset = @"{""type"":""MATCH_STATE"",""version"":1,""timestamp"":1,""phase"":""WAITING"",
                ""hp"":{""1"":100,""2"":100},""winnerId"":null,""connected"":{},""reason"":""reset""}";
            Assert.That(EngineWireCodec.IsMatchReset(reset), Is.True);
            Assert.That(EngineWireCodec.IsMatchReset(reset.Replace(@"""reset""", "null")), Is.False);
            Assert.That(EngineWireCodec.IsMatchReset(Snapshot), Is.False);
            Assert.That(EngineWireCodec.Decode(reset), Is.Empty, "complete snapshots own online state");
        }

        [Test]
        public void GodModeCommandAndSnapshotFlag()
        {
            // The engine accepts exactly these keys on /control (comms engine/app.py).
            Assert.That(JToken.DeepEquals(JObject.Parse(EngineWireCodec.SetGodMode(true)),
                JObject.Parse(@"{""type"":""SET_GOD_MODE"",""version"":1,""enabled"":true}")), Is.True);
            Assert.That(EngineWireCodec.Decode(Snapshot).OfType<GodModeMessage>().Single().enabled, Is.False,
                "an engine without god mode omits the flag: off");
            var on = Snapshot.Replace(@"""reason"":""NOT_READY"",", @"""reason"":""NOT_READY"",""godMode"":true,");
            Assert.That(EngineWireCodec.Decode(on).OfType<GodModeMessage>().Single().enabled, Is.True);
        }

        [Test]
        public void WeaponLockInProgressComesFromTheSnapshot()
        {
            // No weaponLock (no pose held, or an older engine): not locking, for both players.
            Assert.That(EngineWireCodec.Decode(Snapshot).OfType<WeaponLockMessage>().Select(m => m.locking),
                Is.EqualTo(new[] { false, false }));
            var held = Snapshot.Replace(@"""ready"":false,""position"":null,",
                @"""ready"":false,""position"":null,""weaponLock"":{""weapon"":""MACE"",""heldMs"":1200,""totalMs"":3000},");
            var p2 = EngineWireCodec.Decode(held).OfType<WeaponLockMessage>().Single(m => m.player == PlayerId.P2);
            Assert.That(p2.locking, Is.True);
            Assert.That(p2.weapon, Is.EqualTo(WeaponType.Mace));
            Assert.That(p2.heldSeconds, Is.EqualTo(1.2f).Within(1e-4f));
            Assert.That(p2.totalSeconds, Is.EqualTo(3f).Within(1e-4f));
        }

        [Test]
        public void EnumsUseNamesInsteadOfIncompatibleIntegerValues()
        {
            Assert.That(EngineWireCodec.Weapon("SPEAR"), Is.EqualTo(WeaponType.Spear));
            Assert.That(EngineWireCodec.Action("THRUST"), Is.EqualTo(ActionType.Thrust));
            Assert.That((int)EngineWireCodec.Action("SPIN_ATTACK"), Is.EqualTo(6), "Unity's value, not the wire's");
            Assert.That(EngineWireCodec.Action("SPIN_ATTACK"), Is.EqualTo(ActionType.SpinAttack));
            Assert.That(EngineWireCodec.Action("UPPERCUT"), Is.EqualTo(ActionType.Uppercut));
            Assert.Throws<FormatException>(() => EngineWireCodec.Action("SOMETHING_NEW"));
        }

        [Test]
        public void PositionUsesHorizontalMetresMonotonicMillisecondsAndNormalizedHeading()
        {
            var p = JObject.Parse(EngineWireCodec.Position(PlayerId.P2, new Vector3(2, 1.7f, -3), -90, 123456));
            Assert.That((float)p["x"], Is.EqualTo(2));
            Assert.That((float)p["y"], Is.EqualTo(-3));
            Assert.That((float)p["heading"], Is.EqualTo(180));
            Assert.That((long)p["timestamp"], Is.EqualTo(123456));
            Assert.That((int)p["playerId"], Is.EqualTo(2));
            Assert.That(EngineWireCodec.UnityHeading(0), Is.EqualTo(90));
        }

        [Test]
        public void SnapshotRestoresStateWithoutAttackEvents()
        {
            var output = EngineWireCodec.Decode(Snapshot);
            Assert.That(output.OfType<ActionMessage>(), Is.Empty);
            Assert.That(output.OfType<CombatOutcomeMessage>(), Is.Empty);
            Assert.That(output.OfType<HealthMessage>().Select(m => m.health), Is.EqualTo(new[] { 90, 80 }));
            Assert.That(output.OfType<GuardMessage>().Last().guarding, Is.True);
            Assert.That(output.OfType<WeaponChangedMessage>().Last().selected, Is.False);
            var position = output.OfType<PositionMessage>().Single();
            Assert.That(position.position.y, Is.EqualTo(0)); // avatar feet, not phone height
            Assert.That(position.position.z, Is.EqualTo(3));
            var cooldown = output.OfType<CooldownMessage>().First();
            Assert.That(cooldown.seconds, Is.EqualTo(.5f));
            Assert.That(cooldown.totalSeconds, Is.EqualTo(3.2f));
            Assert.That(cooldown.paused, Is.True);
            Assert.That(cooldown.entries.Single().action, Is.EqualTo(ActionType.Thrust));
            Assert.That(output.OfType<MatchStateMessage>().Single().phase, Is.EqualTo(MatchPhase.Paused));
        }

        [Test]
        public void PureIdleSnapshotClearsGuard()
        {
            var root = JObject.Parse(Snapshot);
            root["players"][1]["guardState"] = "IDLE";
            Assert.That(EngineWireCodec.Decode(root.ToString()).OfType<GuardMessage>().Last().guarding, Is.False);
        }

        [Test]
        public void CombatAnimatesOnceAndRecognitionDoesNotDuplicateIt()
        {
            var output = EngineWireCodec.Decode(@"{""type"":""COMBAT_EVENT"",""version"":1,""timestamp"":10,
                ""attackerId"":1,""defenderId"":2,""weapon"":""SWORD"",""action"":""HORIZONTAL_SWING"",
                ""outcome"":""HIT"",""damage"":10,""attackerHp"":100,""defenderHp"":90,""rangeM"":1}");
            Assert.That(output.OfType<ActionMessage>().Count(), Is.EqualTo(1));
            Assert.That(output.OfType<CombatOutcomeMessage>().Single().player, Is.EqualTo(PlayerId.P2));
        }

        static string Recognized(string action, string outcome, string guard = "IDLE") =>
            $@"{{""type"":""ACTION_RECOGNIZED"",""version"":1,""timestamp"":10,""playerId"":2,""weapon"":""SWORD"",
                ""action"":""{action}"",""actionConfidence"":0.9,""guardState"":""{guard}"",""guardConfidence"":0.0,
                ""outcome"":""{outcome}"",""targetId"":null}}";

        [Test]
        public void ActionRecognized_DecodesForTheFirstPersonView()
        {
            var thrust = EngineWireCodec.Decode(Recognized("THRUST", "MISS")).Cast<ActionRecognizedMessage>().Single();
            Assert.That(thrust.player, Is.EqualTo(PlayerId.P2));
            Assert.That(thrust.action, Is.EqualTo(ActionType.Thrust));
            Assert.That(thrust.onCooldown, Is.False);

            Assert.That(EngineWireCodec.Decode(Recognized("UPPERCUT", "ON_COOLDOWN")).Cast<ActionRecognizedMessage>().Single().onCooldown, Is.True);
            // A guard-only report animates nothing: guard arrives in snapshots.
            Assert.That(EngineWireCodec.Decode(Recognized("IDLE", "STATE_ONLY", guard: "GUARDING")), Is.Empty);
        }

        [Test]
        public void InvalidSnapshotIsRejectedAsAWhole()
        {
            var root = JObject.Parse(Snapshot);
            root["players"][1]["playerId"] = 1;
            Assert.Throws<FormatException>(() => EngineWireCodec.Decode(root.ToString()));
            root = JObject.Parse(Snapshot);
            root["version"] = 2;
            Assert.Throws<FormatException>(() => EngineWireCodec.Decode(root.ToString()));
        }
    }
}
