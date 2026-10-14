using System;
using System.Collections.Generic;
using CG4002.Combat;
using UnityEngine;

namespace CG4002.State
{
    /// <summary>What the visualizer knows about one player.</summary>
    [Serializable]
    public class PlayerState
    {
        public int health = 100;
        public int maxHealth = 100;
        public WeaponType weapon;
        public bool weaponSelected = true;
        public bool weaponInitialized;
        public bool guarding;
        public bool hasPosition;
        public bool cooldownPaused;
        public float pausedCooldownRemaining;
        public string cooldownActionName;
        public readonly Dictionary<ActionType, CooldownValue> actionCooldowns = new();
        public float cooldownReceivedAt;

        [Tooltip("Time.time at which this player can attack again.")]
        public float cooldownEndsAt;

        [Tooltip("Arena coordinates, metres.")]
        public Vector3 position;
        public float headingDegrees;

        public float CooldownRemaining => cooldownPaused ? pausedCooldownRemaining : Mathf.Max(0f, cooldownEndsAt - Time.time);

        public float RemainingFor(ActionType action) => actionCooldowns.TryGetValue(action, out var value)
            ? Mathf.Max(0f, value.remainingSeconds - (cooldownPaused ? 0f : Time.time - cooldownReceivedAt)) : 0f;
    }

    /// <summary>
    /// Where state lives. Holds the latest known state of both players and the match, and
    /// announces every change. Only MessageDispatcher calls Apply; everything else reads the
    /// properties or listens to the events. It is a component so the values can be watched
    /// live in the Inspector.
    /// </summary>
    public class GameState : MonoBehaviour
    {
        [Tooltip("Which player this phone belongs to.")]
        [SerializeField] PlayerId localPlayer = PlayerId.P1;

        [SerializeField] PlayerState player1 = new();
        [SerializeField] PlayerState player2 = new();
        [SerializeField] MatchPhase phase = MatchPhase.Waiting;
        [SerializeField] PlayerId winner;

        public event Action<PlayerId, int> HealthChanged;
        public event Action<PlayerId, WeaponType> WeaponChanged;
        public event Action<PlayerId, bool> GuardChanged;
        public event Action<PlayerId, float> CooldownStarted;
        public event Action<PlayerId, Vector3, float> PositionChanged;
        public event Action<MatchPhase> PhaseChanged;

        public PlayerId LocalPlayer => localPlayer;
        public PlayerId OpponentPlayer => localPlayer == PlayerId.P1 ? PlayerId.P2 : PlayerId.P1;
        public PlayerState Self => Get(LocalPlayer);
        public PlayerState Opponent => Get(OpponentPlayer);
        public MatchPhase Phase => phase;
        public PlayerId Winner => winner;

        public PlayerState Get(PlayerId id) => id == PlayerId.P1 ? player1 : player2;

        public void ConfigureLocalPlayer(PlayerId id) => localPlayer = id;

        /// <summary>Update state from a message. Called only by MessageDispatcher.</summary>
        public void Apply(Message message)
        {
            var player = Get(message.player);

            switch (message)
            {
                case WeaponChangedMessage m:
                    if (m.onlyIfChanged && player.weaponInitialized && player.weapon == m.weapon && player.weaponSelected == m.selected)
                        break;
                    // Always announced, even if unchanged: the default weapon equals the first
                    // one sent at match start, and the avatar still needs to be told to equip it.
                    player.weapon = m.weapon;
                    player.weaponSelected = m.selected;
                    player.weaponInitialized = true;
                    WeaponChanged?.Invoke(m.player, m.weapon);
                    break;

                case GuardMessage m:
                    // Only announce real changes: a repeated "still guarding" must not restart
                    // the guard animation.
                    if (player.guarding != m.guarding)
                    {
                        player.guarding = m.guarding;
                        GuardChanged?.Invoke(m.player, m.guarding);
                    }
                    break;

                case CombatOutcomeMessage m:
                    SetHealth(m.player, m.defenderHealth);
                    break;

                case HealthMessage m:
                    player.maxHealth = m.maxHealth;
                    SetHealth(m.player, m.health);
                    break;

                case CooldownMessage m:
                    player.cooldownEndsAt = Time.time + m.seconds;
                    player.cooldownPaused = m.paused;
                    player.pausedCooldownRemaining = m.seconds;
                    player.cooldownActionName = m.actionName;
                    player.cooldownReceivedAt = Time.time;
                    if (m.entries != null)
                    {
                        player.actionCooldowns.Clear();
                        foreach (var entry in m.entries) player.actionCooldowns[entry.action] = entry;
                    }
                    CooldownStarted?.Invoke(m.player, m.totalSeconds > 0f ? m.totalSeconds : m.seconds);
                    break;

                case PositionMessage m:
                    player.hasPosition = true;
                    player.position = m.position;
                    player.headingDegrees = m.headingDegrees;
                    PositionChanged?.Invoke(m.player, m.position, m.headingDegrees);
                    break;

                case MatchStateMessage m:
                    phase = m.phase;
                    winner = m.winner;
                    PhaseChanged?.Invoke(m.phase);
                    break;

                // ActionMessage carries no state: it is an event only, raised by the dispatcher.
            }
        }

        void SetHealth(PlayerId id, int health)
        {
            var player = Get(id);
            if (player.health == health)
                return;
            player.health = health;
            HealthChanged?.Invoke(id, health);
        }
    }
}
