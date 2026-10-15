using System;
using CG4002.Combat;
using UnityEngine;

namespace CG4002.State
{
    // Every message crossing the visualizer's boundary. The wire format (JSON field names,
    // type strings) is in Docs/StateEngine.md, "Integration".

    public enum PlayerId
    {
        P1 = 1,
        P2 = 2,
    }

    public enum Outcome
    {
        Hit = 0,
        Miss = 1,
        Block = 2,
        Parry = 3,
    }

    public enum MatchPhase
    {
        Waiting = 0,
        Running = 1,
        Ended = 2,
        Paused = 3,
    }

    /// <summary>Fields every message carries.</summary>
    public abstract class Message
    {
        public const int CurrentSchemaVersion = 1;

        public int schemaVersion = CurrentSchemaVersion;

        /// <summary>When the sender created the message, in Unix seconds.</summary>
        public double timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

        /// <summary>The player this message is about.</summary>
        public PlayerId player;
    }

    // ── Engine to visualizer (Inbound) ─────────────────────────────────────────────

    public class WeaponChangedMessage : Message
    {
        public WeaponType weapon;
        public bool selected = true;
        public bool onlyIfChanged;
    }

    public class GuardMessage : Message
    {
        public bool guarding;
    }

    /// <summary>
    /// A player performed an attack; <see cref="Message.player"/> is the attacker.
    /// Inbound it drives the attack animation. Outbound it is sent only in offline mode, by
    /// SwordHitDetector standing in for the IMU and classifier.
    /// </summary>
    public class ActionMessage : Message
    {
        public ActionType action;
    }

    /// <summary>
    /// Result of an attack; <see cref="Message.player"/> is the defender. Carries the
    /// defender's resulting health, not just the damage, so a lost message can't leave
    /// health permanently wrong.
    /// </summary>
    public class CombatOutcomeMessage : Message
    {
        public PlayerId attacker;
        public Outcome outcome;
        public int damage;
        public int defenderHealth;
    }

    /// <summary>Sets a player's health outright, e.g. at match start.</summary>
    public class HealthMessage : Message
    {
        public int health;
        public int maxHealth;
    }

    /// <summary>The player cannot attack again for this many seconds.</summary>
    public class CooldownMessage : Message
    {
        public float seconds;
        public float totalSeconds;
        public bool paused;
        public string actionName;
        public CooldownValue[] entries;
    }

    public class CooldownValue
    {
        public ActionType action;
        public float remainingSeconds;
        public float totalSeconds;
    }

    /// <summary>
    /// Where a player stands, in arena coordinates (metres, y up), and which way they face
    /// (degrees clockwise from the arena's forward axis). Goes both ways: outbound for this
    /// phone's player (PositionReporter), inbound for the opponent.
    /// </summary>
    public class PositionMessage : Message
    {
        public long monotonicTimestampMs = -1;
        public Vector3 position;
        public float headingDegrees;
    }

    /// <summary>Match started or ended. <see cref="Message.player"/> is unused.</summary>
    public class MatchStateMessage : Message
    {
        public MatchPhase phase;
        public PlayerId winner;
    }

    // ── Visualizer to engine (Outbound) ────────────────────────────────────────────
    // PositionMessage and, offline only, ActionMessage above are also sent outbound.

    /// <summary>
    /// The player asks for a new match: sent when the arena is placed and when RESET is
    /// pressed. The engine replies with the full starting state (weapon, health, guard for
    /// both players, then MatchState Running).
    /// </summary>
    public class StartMatchRequestMessage : Message
    {
    }
}
