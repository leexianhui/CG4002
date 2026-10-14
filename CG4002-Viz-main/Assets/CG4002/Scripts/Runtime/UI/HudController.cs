using CG4002.Combat;
using CG4002.State;
using UnityEngine;
using UnityEngine.UI;

namespace CG4002.UI
{
    /// <summary>
    /// Screen-space HUD (report 6.1.1): health for both players, the local player's attack
    /// cooldown and weapon, the opponent's guard, and short messages for outcomes and the match
    /// result. Only draws what GameState and the dispatcher announce; it holds no game logic.
    ///
    /// The layout lives in the scene (built by CG4002 > Setup Game Scene) so it can be adjusted
    /// in the editor; this class only binds data to it.
    /// </summary>
    public class HudController : MonoBehaviour
    {
        [SerializeField] GameState gameState;
        [SerializeField] MessageDispatcher dispatcher;

        [Header("You")]
        [SerializeField] RectTransform selfHealthFill;
        [SerializeField] Text selfHealthText;
        [SerializeField] RectTransform cooldownFill;
        [SerializeField] Text weaponText;

        [Header("Opponent")]
        [SerializeField] RectTransform opponentHealthFill;
        [SerializeField] Text opponentHealthText;
        [SerializeField] GameObject opponentGuardIndicator;

        [Header("Messages")]
        [SerializeField] Text outcomeText;
        [SerializeField] float outcomeSeconds = 0.8f;
        [SerializeField] Text resultText;

        float cooldownDuration = 1f;
        float outcomeHideAt;

        void OnEnable()
        {
            gameState.HealthChanged += OnHealthChanged;
            gameState.WeaponChanged += OnWeaponChanged;
            gameState.GuardChanged += OnGuardChanged;
            gameState.CooldownStarted += OnCooldownStarted;
            gameState.PhaseChanged += OnPhaseChanged;
            dispatcher.OutcomeReceived += OnOutcomeReceived;
        }

        void OnDisable()
        {
            gameState.HealthChanged -= OnHealthChanged;
            gameState.WeaponChanged -= OnWeaponChanged;
            gameState.GuardChanged -= OnGuardChanged;
            gameState.CooldownStarted -= OnCooldownStarted;
            gameState.PhaseChanged -= OnPhaseChanged;
            dispatcher.OutcomeReceived -= OnOutcomeReceived;
        }

        void Start()
        {
            // Draw the current state once; after this, events keep it up to date.
            DrawHealth(gameState.LocalPlayer);
            DrawHealth(gameState.OpponentPlayer);
            weaponText.text = WeaponName(gameState.Self.weapon);
            opponentGuardIndicator.SetActive(gameState.Opponent.guarding);
            outcomeText.gameObject.SetActive(false);
            resultText.gameObject.SetActive(false);
        }

        // ---- State ------------------------------------------------------------------------

        void OnHealthChanged(PlayerId player, int health) => DrawHealth(player);

        void DrawHealth(PlayerId player)
        {
            PlayerState state = gameState.Get(player);
            bool isSelf = player == gameState.LocalPlayer;
            SetFill(isSelf ? selfHealthFill : opponentHealthFill, (float)state.health / state.maxHealth);
            (isSelf ? selfHealthText : opponentHealthText).text = $"{state.health} / {state.maxHealth}";
        }

        void OnWeaponChanged(PlayerId player, WeaponType weapon)
        {
            if (player == gameState.LocalPlayer)
                weaponText.text = gameState.Self.weaponSelected ? WeaponName(weapon) : "SELECT WEAPON";
        }

        void OnGuardChanged(PlayerId player, bool guarding)
        {
            if (player == gameState.OpponentPlayer)
                opponentGuardIndicator.SetActive(guarding);
        }

        void OnCooldownStarted(PlayerId player, float seconds)
        {
            if (player == gameState.LocalPlayer)
                cooldownDuration = Mathf.Max(seconds, 0.01f);
        }

        void OnPhaseChanged(MatchPhase phase)
        {
            resultText.gameObject.SetActive(phase == MatchPhase.Ended);
            if (phase == MatchPhase.Ended)
                resultText.text = gameState.Winner == gameState.LocalPlayer ? "DUMMY DEFEATED" : "YOU WERE DEFEATED";
        }

        // ---- Events -----------------------------------------------------------------------

        void OnOutcomeReceived(CombatOutcomeMessage message)
        {
            bool iAttacked = message.attacker == gameState.LocalPlayer;
            outcomeText.text = message.outcome switch
            {
                Outcome.Hit => iAttacked ? $"HIT  -{message.damage}" : $"YOU WERE HIT  -{message.damage}",
                Outcome.Block => iAttacked ? "BLOCKED" : "YOU BLOCKED",
                Outcome.Parry => "PARRIED",
                _ => "MISS",
            };
            outcomeText.color = message.outcome == Outcome.Hit ? new Color(1f, 0.35f, 0.25f) : Color.white;
            outcomeText.gameObject.SetActive(true);
            outcomeHideAt = Time.time + outcomeSeconds;
        }

        void Update()
        {
            // The cooldown bar refills continuously, so it is redrawn every frame rather than
            // on an event. It reads GameState; it doesn't change it.
            SetFill(cooldownFill, 1f - gameState.Self.CooldownRemaining / cooldownDuration);

            if (outcomeText.gameObject.activeSelf && Time.time >= outcomeHideAt)
                outcomeText.gameObject.SetActive(false);
        }

        // ---- Helpers ----------------------------------------------------------------------

        /// <summary>Show a fraction of a bar by moving the fill's right edge (0 = empty, 1 = full).</summary>
        static void SetFill(RectTransform fill, float fraction)
        {
            fill.anchorMax = new Vector2(Mathf.Clamp01(fraction), 1f);
        }

        static string WeaponName(WeaponType weapon) => weapon switch
        {
            WeaponType.SwordAndShield => "SWORD & SHIELD",
            WeaponType.Spear => "SPEAR",
            WeaponType.Mace => "MACE",
            _ => weapon.ToString(),
        };
    }
}
