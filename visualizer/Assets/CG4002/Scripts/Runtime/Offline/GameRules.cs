using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.RegularExpressions;
using CG4002.Combat;
using CG4002.Net;
using UnityEngine;

namespace CG4002.Offline
{
    /// <summary>
    /// The engine's game rules, read from the same files the engine uses: shared/config/weapons.yaml
    /// (per weapon and move: damage, range, cooldown) and match.yaml (HP, block, parry, guard arc).
    /// RulesBuildStep copies them into Resources/Rules before every build, so the demo always plays by
    /// the engine's current numbers. Only the simple shapes those two files use are parsed.
    /// </summary>
    public sealed class GameRules
    {
        public struct Move
        {
            public int damage;
            public float range;     // metres
            public float cooldown;  // seconds
        }

        public readonly Dictionary<WeaponType, Dictionary<ActionType, Move>> moves = new();
        public int startingHp = 100;
        public float parryWindow = 0.15f;
        public float blockMultiplier = 0.25f;
        public float stagger = 0.5f;
        public float guardArcDegrees = 120f;
        public float guardGrace = 0.05f;
        /// <summary>Seconds a raised guard stays up (then drops by itself); 0 = until it's lowered.</summary>
        public float guardDuration;

        public bool TryGetMove(WeaponType weapon, ActionType action, out Move move)
        {
            move = default;
            return moves.TryGetValue(weapon, out var set) && set.TryGetValue(action, out move);
        }

        /// <summary>The rules shipped in the app (Resources/Rules/weapons.txt and match.txt).</summary>
        public static GameRules Load()
        {
            var weapons = Resources.Load<TextAsset>("Rules/weapons");
            var match = Resources.Load<TextAsset>("Rules/match");
            if (weapons == null || match == null)
                throw new InvalidOperationException("Missing Resources/Rules/weapons or match: rebuild to copy shared/config.");
            return Parse(weapons.text, match.text);
        }

        // "      HORIZONTAL_SWING: { damage: 10, range_m: 1.5, cooldown_ms: 2400 }"
        static readonly Regex MoveLine = new(@"^\s+([A-Z_]+):\s*\{(.*)\}");
        // "  SWORD:      # balanced"
        static readonly Regex WeaponLine = new(@"^  ([A-Z_]+):\s*(#.*)?$");
        static readonly Regex Field = new(@"(\w+)\s*:\s*(-?[0-9.]+)");

        public static GameRules Parse(string weaponsYaml, string matchYaml)
        {
            var rules = new GameRules();
            Dictionary<ActionType, Move> current = null;
            foreach (var raw in weaponsYaml.Split('\n'))
            {
                string line = raw.TrimEnd('\r');
                var weapon = WeaponLine.Match(line);
                if (weapon.Success)
                {
                    current = null;
                    try { rules.moves[EngineWireCodec.Weapon(weapon.Groups[1].Value)] = current = new(); }
                    catch (FormatException) { }  // a weapon the visualizer doesn't have
                    continue;
                }
                var move = MoveLine.Match(line);
                if (current == null || !move.Success)
                    continue;
                ActionType action;
                try { action = EngineWireCodec.Action(move.Groups[1].Value); }
                catch (FormatException) { continue; }
                var values = Values(move.Groups[2].Value);
                current[action] = new Move
                {
                    damage = (int)values.GetValueOrDefault("damage"),
                    range = values.GetValueOrDefault("range_m"),
                    cooldown = values.GetValueOrDefault("cooldown_ms") / 1000f,
                };
            }

            var match = Values(matchYaml);
            rules.startingHp = (int)match.GetValueOrDefault("starting_hp", rules.startingHp);
            rules.parryWindow = match.GetValueOrDefault("parry_window_ms", rules.parryWindow * 1000f) / 1000f;
            rules.blockMultiplier = match.GetValueOrDefault("block_multiplier", rules.blockMultiplier);
            rules.stagger = match.GetValueOrDefault("stagger_ms", rules.stagger * 1000f) / 1000f;
            rules.guardArcDegrees = match.GetValueOrDefault("guard_arc_deg", rules.guardArcDegrees);
            rules.guardGrace = match.GetValueOrDefault("guard_grace_ms", rules.guardGrace * 1000f) / 1000f;
            rules.guardDuration = match.GetValueOrDefault("guard_duration_ms", rules.guardDuration * 1000f) / 1000f;
            return rules;
        }

        /// <summary>Every "key: number" pair in the text, ignoring # comments.</summary>
        static Dictionary<string, float> Values(string text)
        {
            var values = new Dictionary<string, float>();
            foreach (var raw in text.Split('\n'))
            {
                string line = raw;
                int comment = line.IndexOf('#');
                if (comment >= 0)
                    line = line.Substring(0, comment);
                foreach (Match field in Field.Matches(line))
                {
                    if (float.TryParse(field.Groups[2].Value, NumberStyles.Float, CultureInfo.InvariantCulture, out float value))
                        values[field.Groups[1].Value] = value;
                }
            }
            return values;
        }
    }
}
