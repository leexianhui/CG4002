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
             ""cooldownAction"":""CHARGED_THRUST"",""cooldowns"":[
               {""action"":""CHARGED_THRUST"",""remainingMs"":500,""totalMs"":3200}]},
            {""playerId"":2,""hp"":80,""maxHp"":100,""weapon"":null,""guardState"":""GUARDING"",
             ""connected"":true,""ready"":false,""position"":null,""cooldownAction"":null,""cooldowns"":[]}] }";

        [Test]
        public void EnumsUseNamesInsteadOfIncompatibleIntegerValues()
        {
            Assert.That(EngineWireCodec.Weapon("SPEAR"), Is.EqualTo(WeaponType.Spear));
            Assert.That(EngineWireCodec.Action("CHARGED_THRUST"), Is.EqualTo(ActionType.ChargedThrust));
            Assert.That((int)EngineWireCodec.Action("CHARGED_THRUST"), Is.EqualTo(7));
            Assert.Throws<FormatException>(() => EngineWireCodec.Action("SPIN_ATTACK"));
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
            Assert.That(cooldown.entries.Single().action, Is.EqualTo(ActionType.ChargedThrust));
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
            Assert.That(EngineWireCodec.Decode(@"{""type"":""ACTION_RECOGNIZED"",""version"":1}"), Is.Empty);
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
