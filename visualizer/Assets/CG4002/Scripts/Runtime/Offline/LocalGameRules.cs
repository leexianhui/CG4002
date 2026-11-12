using System.Collections;
using System.Collections.Generic;
using CG4002.Combat;
using CG4002.State;
using UnityEngine;

namespace CG4002.Offline
{
    /// <summary>
    /// The demo engine: an on-phone stand-in for the Game Engine, used by demo mode (OnlineScene's
    /// DEMO MODE button) and by SampleScene. It sits where the comms client does: it reads the
    /// visualizer's Outbound queue and answers on the Inbound queue with the messages the real engine
    /// sends, owning health, guard and cooldowns and deciding every outcome. It keeps its own copy
    /// of the state rather than reading GameState, because it plays the "server side".
    ///
    /// Outcomes follow the engine's resolver (comms/src/cg_comms/engine/resolver.py), in its order,
    /// with its numbers from shared/config/weapons.yaml and match.yaml (see GameRules):
    ///   match not running, or a move the weapon lacks   ignored
    ///   move still cooling down                         ON_COOLDOWN: recognised, nothing else
    ///   further apart than the move's range_m           MISS
    ///   defender guarding and facing the attacker       PARRY if the guard went up within
    ///                                                   parry_window_ms (0 damage, attacker staggered),
    ///                                                   else BLOCK (damage x block_multiplier, rounded up)
    ///   otherwise                                       HIT, full damage
    /// Every resolved attack starts that move's cooldown (each move only blocks itself).
    ///
    /// Guard, like the engine's hardware guard (game_state_task.py): raising it starts a window of
    /// guard_duration_ms (3 s), after which it drops by itself; raising it again while it's up doesn't
    /// extend it, and lowering it early does nothing (the engine ignores the release). Attacking
    /// drops your own guard. With guard_duration_ms 0 the guard lasts until it's lowered.
    ///
    /// Positions: a player whose camera reports positions (PositionReporter, on Outbound) is where
    /// it reports, facing where it looks. The other (the placed enemy) stands at the arena origin,
    /// or where Move put it, and always faces its opponent.
    ///
    /// Online, this GameObject is disabled and the real engine sends the same messages.
    /// </summary>
    public class LocalGameRules : MonoBehaviour
    {
        class Fighter
        {
            public int health;
            public bool guarding;
            public float guardRaisedAt = float.NegativeInfinity;
            public WeaponType weapon;
            public bool weaponLocked;
            /// <summary>Arena metres (x, z). The heading only counts if the player reports one.</summary>
            public Vector2 position;
            public float headingDegrees;
            public bool reportsHeading;
            /// <summary>Per move, like the engine: when it's ready again, and its full length.</summary>
            public readonly Dictionary<ActionType, (float endsAt, float total)> cooldowns = new();
        }

        [SerializeField] MessageDispatcher dispatcher;

        [Header("Rules")]
        [SerializeField] WeaponType startingWeapon = WeaponType.SwordAndShield;
        [Tooltip("Start each player with no weapon, as the hub does until a stance is held; LockWeapon then selects one.")]
        [SerializeField] bool requireWeaponSelection;
        [Tooltip("Multiplies every move's cooldown (0 turns them off). The cooldowns also stop one swing counting as several hits.")]
        [SerializeField] float cooldownScale = 1f;
        [Tooltip("Seconds after a player is defeated before a new match starts.")]
        [SerializeField] float restartDelay = 3f;

        readonly Fighter player1 = new();
        readonly Fighter player2 = new();
        MatchPhase phase = MatchPhase.Waiting;
        GameRules rules;

        /// <summary>The engine's rules (damage, range, cooldowns, block and parry) this stand-in plays by.</summary>
        public GameRules Rules => rules ??= GameRules.Load();

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
                    case PositionMessage m:
                        // This phone's camera, in arena coordinates: used for range and facing.
                        var fighter = Get(m.player);
                        fighter.position = new Vector2(m.position.x, m.position.z);
                        fighter.headingDegrees = m.headingDegrees;
                        fighter.reportsHeading = true;
                        break;
                }
            }
            ExpireGuards();
        }

        /// <summary>A guard that has been up for guard_duration_ms drops, as the engine's snapshot shows it.</summary>
        void ExpireGuards()
        {
            foreach (var id in new[] { PlayerId.P1, PlayerId.P2 })
            {
                var fighter = Get(id);
                if (fighter.guarding && !GuardActive(fighter))
                    DropGuard(id);
            }
        }

        bool GuardActive(Fighter fighter) =>
            fighter.guarding && (Rules.guardDuration <= 0f || Time.time - fighter.guardRaisedAt <= Rules.guardDuration);

        void DropGuard(PlayerId player)
        {
            Get(player).guarding = false;
            Send(new GuardMessage { player = player, guarding = false });
        }

        public void StartMatch()
        {
            foreach (var id in new[] { PlayerId.P1, PlayerId.P2 })
            {
                var fighter = Get(id);
                fighter.health = Rules.startingHp;
                fighter.guarding = false;
                fighter.guardRaisedAt = float.NegativeInfinity;
                fighter.cooldowns.Clear();
                if (!fighter.weaponLocked)
                    fighter.weapon = startingWeapon;
                Send(new WeaponChangedMessage { player = id, weapon = fighter.weapon,
                    selected = !requireWeaponSelection || fighter.weaponLocked });
                Send(new CooldownMessage { player = id, entries = System.Array.Empty<CooldownValue>() });
                Send(new HealthMessage { player = id, health = Rules.startingHp, maxHealth = Rules.startingHp });
                Send(new GuardMessage { player = id, guarding = false });
            }
            StopAllCoroutines();  // a pending restart from the last match
            phase = MatchPhase.Running;
            Send(new MatchStateMessage { phase = MatchPhase.Running });
        }

        /// <summary>
        /// Demo mode: both players start without a weapon (the selection screen asks for yours, then
        /// the enemy's) and a fresh match begins.
        /// </summary>
        public void BeginWeaponSelection()
        {
            requireWeaponSelection = true;
            player1.weaponLocked = player2.weaponLocked = false;
            StartMatch();
        }

        /// <summary>
        /// A player attacks the other, decided like the engine's resolver (see the class summary).
        /// Returns false if the attack wasn't resolved: match not running, move on cooldown, or a
        /// move this weapon doesn't have.
        /// </summary>
        public bool Attack(PlayerId attacker, ActionType action)
        {
            var attackerState = Get(attacker);
            // Attacking drops your own guard, so nobody can keep a guard up and swing without risk.
            if (attackerState.guarding)
                DropGuard(attacker);
            bool onCooldown = attackerState.cooldowns.TryGetValue(action, out var running) && Time.time < running.endsAt;
            // Like the engine's ACTION_RECOGNIZED: every attack is reported, resolved or not.
            Send(new ActionRecognizedMessage { player = attacker, action = action, onCooldown = onCooldown });

            if (phase != MatchPhase.Running || onCooldown || !Rules.TryGetMove(attackerState.weapon, action, out var move))
                return false;

            PlayerId defender = OtherPlayer(attacker);
            var defenderState = Get(defender);

            Outcome outcome;
            int damage = 0;
            if (Vector2.Distance(attackerState.position, defenderState.position) > move.range)
                outcome = Outcome.Miss;
            else if (GuardActive(defenderState) && Faces(defenderState, attackerState))
            {
                bool parried = Time.time - defenderState.guardRaisedAt <= Rules.parryWindow;
                outcome = parried ? Outcome.Parry : Outcome.Block;
                damage = parried ? 0 : Mathf.CeilToInt(move.damage * Rules.blockMultiplier);
            }
            else
            {
                outcome = Outcome.Hit;
                damage = move.damage;
            }

            defenderState.health = Mathf.Max(0, defenderState.health - damage);
            float cooldown = move.cooldown * cooldownScale;
            attackerState.cooldowns[action] = (Time.time + cooldown, cooldown);
            if (outcome == Outcome.Parry)
                Stagger(attackerState, Rules.stagger * cooldownScale);

            Send(new ActionMessage { player = attacker, action = action });
            Send(new CombatOutcomeMessage
            {
                player = defender,
                attacker = attacker,
                outcome = outcome,
                damage = damage,
                defenderHealth = defenderState.health,
            });
            Send(RunningCooldowns(attacker, action, cooldown));

            if (defenderState.health == 0)
                EndMatch(winner: attacker);
            return true;
        }

        /// <summary>
        /// Whether the defender faces the attacker within the guard arc. One that doesn't report a
        /// heading (the placed enemy) always faces its opponent.
        /// </summary>
        bool Faces(Fighter defender, Fighter attacker)
        {
            Vector2 toAttacker = attacker.position - defender.position;
            if (!defender.reportsHeading || toAttacker.sqrMagnitude < 1e-6f)
                return true;
            // Headings are clockwise from the arena's +Z, looking down (PositionReporter.ToArena).
            float bearing = Mathf.Atan2(toAttacker.x, toAttacker.y) * Mathf.Rad2Deg;
            return Mathf.Abs(Mathf.DeltaAngle(defender.headingDegrees, bearing)) <= Rules.guardArcDegrees / 2f;
        }

        /// <summary>A parried attacker: every cooldown still running gets longer (engine add_stagger).</summary>
        static void Stagger(Fighter attacker, float seconds)
        {
            foreach (var (move, (endsAt, total)) in new List<KeyValuePair<ActionType, (float, float)>>(attacker.cooldowns))
            {
                if (endsAt > Time.time)
                    attacker.cooldowns[move] = (endsAt + seconds, total + seconds);
            }
        }

        /// <summary>Every move still cooling down, like the engine's snapshot (`just` is the move that started one).</summary>
        CooldownMessage RunningCooldowns(PlayerId player, ActionType just, float seconds)
        {
            var entries = new List<CooldownValue>();
            foreach (var (move, (endsAt, total)) in Get(player).cooldowns)
            {
                if (endsAt > Time.time)
                    entries.Add(new CooldownValue { action = move, remainingSeconds = endsAt - Time.time, totalSeconds = total });
            }
            return new CooldownMessage { player = player, seconds = seconds, totalSeconds = seconds,
                actionName = just.ToString(), entries = entries.ToArray() };
        }

        /// <summary>What the hub reports once a stance has been held: this player's weapon is chosen.</summary>
        public void LockWeapon(PlayerId player, WeaponType weapon)
        {
            Get(player).weaponLocked = true;
            Get(player).weapon = weapon;
            Send(new WeaponChangedMessage { player = player, weapon = weapon });
        }

        /// <summary>Demo mode's CHANGE WEAPONS: both players choose again, and a new match starts after.</summary>
        public void UnlockWeapons()
        {
            foreach (var id in new[] { PlayerId.P1, PlayerId.P2 })
            {
                Get(id).weaponLocked = false;
                Send(new WeaponChangedMessage { player = id, weapon = Get(id).weapon, selected = false });
            }
        }

        /// <summary>
        /// The guard pose was raised (true) or lowered (false), as the wearable reports it. Raising
        /// starts a guard_duration_ms window (not extended while it's up); lowering only counts when
        /// guard_duration_ms is 0, since the engine otherwise ignores the release.
        /// </summary>
        public void SetGuard(PlayerId player, bool guarding)
        {
            var fighter = Get(player);
            if (guarding)
            {
                if (GuardActive(fighter))
                    return;
                fighter.guarding = true;
                fighter.guardRaisedAt = Time.time;  // a hit within the parry window of this is parried
                Send(new GuardMessage { player = player, guarding = true });
            }
            else if (fighter.guarding && Rules.guardDuration <= 0f)
                DropGuard(player);
        }

        /// <summary>Moves a player (the enemy, side-stepping), in arena coordinates. It keeps facing its opponent.</summary>
        public void Move(PlayerId player, Vector3 position, float headingDegrees)
        {
            Get(player).position = new Vector2(position.x, position.z);
            Send(new PositionMessage { player = player, position = position, headingDegrees = headingDegrees });
        }

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
