using System;
using System.Collections.Generic;
using CG4002.Combat;
using CG4002.State;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace CG4002.Net
{
    /// <summary>Wire v1 to presentation messages. Explicit names preserve serialized Unity enums.</summary>
    public static class EngineWireCodec
    {
        static readonly Dictionary<string, ActionType> Actions = new()
        {
            ["IDLE"] = ActionType.Idle, ["HORIZONTAL_SWING"] = ActionType.HorizontalSwing,
            ["VERTICAL_SWING"] = ActionType.VerticalSwing, ["DIAGONAL_SWING"] = ActionType.DiagonalSwing,
            ["THRUST"] = ActionType.Thrust, ["CHARGED_ATTACK"] = ActionType.ChargedAttack,
            ["CHARGED_THRUST"] = ActionType.ChargedThrust, ["SWING"] = ActionType.Swing,
            // Classified by the AI but not yet in the engine's ActionClass (the AI server reports
            // them as IDLE); decoded here so they animate as soon as the engine sends them.
            ["SPIN_ATTACK"] = ActionType.SpinAttack, ["UPPERCUT"] = ActionType.Uppercut,
        };

        public static ActionType Action(string name) => Actions.TryGetValue(name, out var value)
            ? value : throw new FormatException("Unknown action: " + name);

        public static WeaponType Weapon(string name) => name switch
        {
            "SWORD" => WeaponType.SwordAndShield, "SPEAR" => WeaponType.Spear, "MACE" => WeaponType.Mace,
            _ => throw new FormatException("Unknown weapon: " + name),
        };

        static PlayerId Player(JToken value)
        {
            int id = value?.Value<int>() ?? 0;
            return id == 1 || id == 2 ? (PlayerId)id : throw new FormatException("Invalid player");
        }

        static string Text(JToken obj, string key) => obj[key]?.Type == JTokenType.String
            ? obj[key].Value<string>() : throw new FormatException("Missing string: " + key);

        static float Number(JToken obj, string key)
        {
            var token = obj[key];
            if (token == null || (token.Type != JTokenType.Float && token.Type != JTokenType.Integer))
                throw new FormatException("Missing number: " + key);
            float n = token.Value<float>();
            if (float.IsNaN(n) || float.IsInfinity(n)) throw new FormatException("Non-finite " + key);
            return n;
        }

        public static float CanonicalHeading(float yaw) => ((90f - yaw) % 360f + 360f) % 360f;
        public static float UnityHeading(float heading) => CanonicalHeading(heading);

        public static string Position(PlayerId player, Vector3 calibratedPosition, float yaw, long timestamp) =>
            JsonConvert.SerializeObject(new { type = "POSITION", version = 1, timestamp,
                playerId = (int)player, x = calibratedPosition.x, y = calibratedPosition.z,
                heading = CanonicalHeading(yaw) });

        public static string Ready(PlayerId player, bool ready, long timestamp) =>
            JsonConvert.SerializeObject(new { type = "VISUALIZER_READY", version = 1, timestamp,
                playerId = (int)player, ready });

        public static List<Message> Decode(string json)
        {
            var root = JObject.Parse(json);
            if (root["version"]?.Value<int>() != 1) throw new FormatException("Unsupported wire version");
            var output = new List<Message>();
            switch (Text(root, "type"))
            {
                case "STATE_SNAPSHOT": DecodeSnapshot(root, output); break;
                case "COMBAT_EVENT":
                    var attacker = Player(root["attackerId"]);
                    var defender = Player(root["defenderId"]);
                    var action = Action(Text(root, "action"));
                    if (action == ActionType.Idle) throw new FormatException("Combat cannot be IDLE");
                    var outcome = Text(root, "outcome") switch
                    {
                        "HIT" => Outcome.Hit, "MISS" => Outcome.Miss,
                        "BLOCK" => Outcome.Block, "PARRY" => Outcome.Parry,
                        _ => throw new FormatException("Unknown outcome"),
                    };
                    // COMBAT_EVENT is the sole attack-animation source. Recognition is diagnostic.
                    output.Add(new ActionMessage { player = attacker, action = action });
                    output.Add(new CombatOutcomeMessage { player = defender, attacker = attacker,
                        outcome = outcome, damage = (int)Number(root, "damage"),
                        defenderHealth = (int)Number(root, "defenderHp") });
                    break;
                // These are also sent for legacy clients. Complete snapshots own online state.
                case "MATCH_STATE": case "WEAPON_CHANGED": case "ACTION_RECOGNIZED":
                case "COOLDOWN_STATE": case "POSITION": break;
                default: throw new FormatException("Unknown engine message");
            }
            return output;
        }

        static void DecodeSnapshot(JObject root, List<Message> output)
        {
            var phase = Text(root, "phase") switch
            {
                "WAITING" or "ALIGNING" => MatchPhase.Waiting,
                "IN_PROGRESS" => MatchPhase.Running, "PAUSED" => MatchPhase.Paused,
                "ENDED" => MatchPhase.Ended, _ => throw new FormatException("Unknown phase"),
            };
            var players = root["players"] as JArray;
            if (players == null || players.Count != 2) throw new FormatException("Expected two players");
            var seen = new HashSet<PlayerId>();
            foreach (var p in players)
            {
                var id = Player(p["playerId"]);
                if (!seen.Add(id)) throw new FormatException("Duplicate player");
                string weapon = p["weapon"]?.Value<string>();
                output.Add(new WeaponChangedMessage { player = id, selected = weapon != null,
                    weapon = weapon == null ? default : Weapon(weapon), onlyIfChanged = true });
                output.Add(new HealthMessage { player = id, health = (int)Number(p, "hp"),
                    maxHealth = (int)Number(p, "maxHp") });
                string guard = Text(p, "guardState");
                if (guard != "IDLE" && guard != "GUARDING") throw new FormatException("Unknown guard");
                output.Add(new GuardMessage { player = id, guarding = guard == "GUARDING" });
                if (p["position"] is JObject position)
                    output.Add(new PositionMessage { player = id,
                        position = new Vector3(Number(position, "x"), 0f, Number(position, "y")),
                        headingDegrees = UnityHeading(Number(position, "heading")) });

                var entries = new List<CooldownValue>();
                string displayed = p["cooldownAction"]?.Value<string>();
                if (displayed != null) Action(displayed);
                float remaining = 0, total = 0;
                if (!(p["cooldowns"] is JArray cooldowns)) throw new FormatException("Missing cooldowns");
                foreach (var c in cooldowns)
                {
                    string name = Text(c, "action");
                    var value = new CooldownValue { action = Action(name),
                        remainingSeconds = Number(c, "remainingMs") / 1000f,
                        totalSeconds = Number(c, "totalMs") / 1000f };
                    entries.Add(value);
                    if (name == displayed) { remaining = value.remainingSeconds; total = value.totalSeconds; }
                }
                output.Add(new CooldownMessage { player = id, seconds = remaining, totalSeconds = total,
                    actionName = displayed, paused = phase == MatchPhase.Paused, entries = entries.ToArray() });
            }
            output.Add(new MatchStateMessage { phase = phase,
                winner = root["winnerId"]?.Type == JTokenType.Null ? default : Player(root["winnerId"]) });
        }
    }
}
