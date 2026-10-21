using System.Collections;
using CG4002.Combat;
using CG4002.State;
using UnityEngine;

namespace CG4002.Offline
{
    /// <summary>
    /// Offline stand-in for the Game Engine. It sits where the comms client will: it reads the
    /// visualizer's Outbound queue and answers on the Inbound queue, owning health, guard and
    /// cooldowns and deciding every outcome. It keeps its own copy of the state rather than
    /// reading GameState, because it plays the "server side".
    ///
    /// Online, this GameObject is disabled and the real engine sends the same messages.
    /// </summary>
    public class LocalGameRules : MonoBehaviour
    {
        class Fighter
        {
            public int health;
            public bool guarding;
            public float cooldownEndsAt;
            public bool weaponLocked;
        }

        [SerializeField] MessageDispatcher dispatcher;

        [Header("Rules")]
        [SerializeField] WeaponType startingWeapon = WeaponType.SwordAndShield;
        [Tooltip("Start each player with no weapon, as the hub does until a stance is held; LockWeapon then selects one.")]
        [SerializeField] bool requireWeaponSelection;
        [SerializeField] int maxHealth = 100;
        [SerializeField] int damagePerHit = 10;
        [Tooltip("Seconds after an attack before the same player can attack again. Also stops one swing counting as several hits.")]
        [SerializeField] float attackCooldown = 0.6f;
        [Tooltip("Seconds after a player is defeated before a new match starts.")]
        [SerializeField] float restartDelay = 3f;

        readonly Fighter player1 = new();
        readonly Fighter player2 = new();
        MatchPhase phase = MatchPhase.Waiting;

        Fighter Get(PlayerId id) => id == PlayerId.P1 ? player1 : player2;
        static PlayerId OtherPlayer(PlayerId id) => id == PlayerId.P1 ? PlayerId.P2 : PlayerId.P1;

        void Start() => StartMatch();

        // What the visualizer sends, handled the way the Game Engine would.
        void Update()
        {
            while (dispatcher.Outbound.TryDequeue(out var message))
            {
                switch (message)
                {
                    case StartMatchRequestMessage:
                        StartMatch();
                        break;
                    case ActionMessage m:
                        Attack(m.player, m.action);
                        break;
                    // PositionMessage: no range rule offline, so the position isn't needed.
                }
            }
        }

        public void StartMatch()
        {
            foreach (var id in new[] { PlayerId.P1, PlayerId.P2 })
            {
                var fighter = Get(id);
                fighter.health = maxHealth;
                fighter.guarding = false;
                fighter.cooldownEndsAt = 0f;
                Send(new WeaponChangedMessage { player = id, weapon = startingWeapon,
                    selected = !requireWeaponSelection || fighter.weaponLocked });
                Send(new HealthMessage { player = id, health = maxHealth, maxHealth = maxHealth });
                Send(new GuardMessage { player = id, guarding = false });
            }
            phase = MatchPhase.Running;
            Send(new MatchStateMessage { phase = MatchPhase.Running });
        }

        /// <summary>
        /// A player attacks the other: a guarding defender blocks, otherwise it's a hit.
        /// Returns false if the attack wasn't allowed (match not running, or on cooldown).
        /// </summary>
        public bool Attack(PlayerId attacker, ActionType action)
        {
            if (phase != MatchPhase.Running)
                return false;

            var attackerState = Get(attacker);
            if (Time.time < attackerState.cooldownEndsAt)
                return false;

            PlayerId defender = OtherPlayer(attacker);
            var defenderState = Get(defender);

            Outcome outcome = defenderState.guarding ? Outcome.Block : Outcome.Hit;
            int damage = outcome == Outcome.Hit ? damagePerHit : 0;
            defenderState.health = Mathf.Max(0, defenderState.health - damage);
            attackerState.cooldownEndsAt = Time.time + attackCooldown;

            Send(new ActionMessage { player = attacker, action = action });
            Send(new CombatOutcomeMessage
            {
                player = defender,
                attacker = attacker,
                outcome = outcome,
                damage = damage,
                defenderHealth = defenderState.health,
            });
            Send(new CooldownMessage { player = attacker, seconds = attackCooldown });

            if (defenderState.health == 0)
                EndMatch(winner: attacker);
            return true;
        }

        /// <summary>What the hub reports once a stance has been held: this player's weapon is chosen.</summary>
        public void LockWeapon(PlayerId player, WeaponType weapon)
        {
            Get(player).weaponLocked = true;
            Send(new WeaponChangedMessage { player = player, weapon = weapon });
        }

        public void SetGuard(PlayerId player, bool guarding)
        {
            var fighter = Get(player);
            if (fighter.guarding == guarding)
                return;
            fighter.guarding = guarding;
            Send(new GuardMessage { player = player, guarding = guarding });
        }

        /// <summary>Moves a player, in arena coordinates.</summary>
        public void Move(PlayerId player, Vector3 position, float headingDegrees) =>
            Send(new PositionMessage { player = player, position = position, headingDegrees = headingDegrees });

        public bool IsGuarding(PlayerId player) => Get(player).guarding;

        void EndMatch(PlayerId winner)
        {
            phase = MatchPhase.Ended;
            Send(new MatchStateMessage { phase = MatchPhase.Ended, winner = winner });
            StartCoroutine(RestartAfterDelay());
        }

        IEnumerator RestartAfterDelay()
        {
            yield return new WaitForSeconds(restartDelay);
            StartMatch();
        }

        void Send(Message message) => dispatcher.Inbound.Enqueue(message);
    }
}
