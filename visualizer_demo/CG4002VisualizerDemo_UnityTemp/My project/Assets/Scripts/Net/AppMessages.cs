// C# mirrors of shared/protocol/app_messages.md's JSON messages — only the subset a visualizer
// receives/sends (COMBAT_EVENT, MATCH_STATE, COOLDOWN_STATE, WEAPON_CHANGED, POSITION). Field
// names use JsonProperty to match the wire's camelCase exactly; do not rename without updating
// both this file and app_messages.md in the same change.

using System.Collections.Generic;
using Newtonsoft.Json;

namespace Cg4002.VisualizerDemo.Net
{
    // Every message on the wire carries at least these three fields (app_messages.md §1).
    public class Envelope
    {
        [JsonProperty("type")] public string Type;
        [JsonProperty("version")] public int Version;
        [JsonProperty("timestamp")] public long Timestamp;
    }

    public class WeaponChanged : Envelope
    {
        [JsonProperty("playerId")] public int PlayerId;
        [JsonProperty("weapon")] public string Weapon;
    }

    public class Position : Envelope
    {
        [JsonProperty("playerId")] public int PlayerId;
        [JsonProperty("x")] public float X;
        [JsonProperty("y")] public float Y;
        [JsonProperty("heading")] public float Heading;
    }

    public class CombatEvent : Envelope
    {
        [JsonProperty("attackerId")] public int AttackerId;
        [JsonProperty("defenderId")] public int DefenderId;
        [JsonProperty("weapon")] public string Weapon;
        [JsonProperty("action")] public string Action;
        [JsonProperty("outcome")] public string Outcome;
        [JsonProperty("damage")] public int Damage;
        [JsonProperty("attackerHp")] public int AttackerHp;
        [JsonProperty("defenderHp")] public int DefenderHp;
        [JsonProperty("rangeM")] public float RangeM;
    }

    public class MatchState : Envelope
    {
        [JsonProperty("phase")] public string Phase;
        [JsonProperty("hp")] public Dictionary<string, int> Hp;
        [JsonProperty("winnerId")] public int? WinnerId;
        [JsonProperty("reason")] public string Reason;
        [JsonProperty("connected")] public Dictionary<string, bool> Connected;
    }

    // Reports what was recognised regardless of combat outcome - distinct from CombatEvent, which
    // only fires for MISS/HIT/BLOCK/PARRY (shared/protocol/app_messages.md, DEMO_TRIGGERED_ACTION_PLAN
    // §7.2). Guard is action=IDLE + guardState=GUARDING, not a member of ActionClass.
    public class ActionRecognized : Envelope
    {
        [JsonProperty("playerId")] public int PlayerId;
        [JsonProperty("weapon")] public string Weapon;
        [JsonProperty("action")] public string Action;
        [JsonProperty("actionConfidence")] public float ActionConfidence;
        [JsonProperty("guardState")] public string GuardState;
        [JsonProperty("guardConfidence")] public float GuardConfidence;
        [JsonProperty("outcome")] public string Outcome;
        [JsonProperty("targetId")] public int? TargetId;
    }

    public class CooldownEntry
    {
        [JsonProperty("action")] public string Action;
        [JsonProperty("remainingMs")] public int RemainingMs;
        [JsonProperty("totalMs")] public int TotalMs;
    }

    public class CooldownState : Envelope
    {
        [JsonProperty("playerId")] public int PlayerId;
        [JsonProperty("cooldowns")] public List<CooldownEntry> Cooldowns;
    }

    // Minimal helper for peeking "type" before picking a concrete class to deserialize into.
    public static class AppMessage
    {
        public static string PeekType(string json)
        {
            var envelope = JsonConvert.DeserializeObject<Envelope>(json);
            return envelope?.Type;
        }
    }
}
