using CG4002.Combat;
using CG4002.State;
using UnityEngine;
using UnityEngine.UI;

namespace CG4002.UI
{
    /// <summary>
    /// Screen-space HUD (report 6.1.1), medieval-styled: health for both players, the local player's
    /// weapon and a cooldown icon per move (bottom left), the opponent's guard, and short messages for outcomes
    /// and the match result. Only draws what GameState and the dispatcher announce; it holds no game logic.
    ///
    /// The layout lives in the scene (built by CG4002 > Setup Game Scene) so it can be adjusted
    /// in the editor; this class only binds data to it.
    /// </summary>
    public class HudController : MonoBehaviour
    {
        /// <summary>
        /// One move's cooldown: its icon, covered by a grey radial overlay (an Image of type Filled)
        /// that sweeps away as the move becomes ready. Each move cools down on its own.
        /// </summary>
        [System.Serializable]
        public class CooldownRow
        {
            public ActionType action;
            public Image overlay;
        }

        [SerializeField] GameState gameState;
        [SerializeField] MessageDispatcher dispatcher;

        [Header("You")]
        [SerializeField] RectTransform selfHealthFill;
        [SerializeField] Text selfHealthText;
        [SerializeField] Text weaponText;

        [Header("Cooldowns (bottom left)")]
        [SerializeField] CooldownRow[] cooldownRows = System.Array.Empty<CooldownRow>();

        [Header("Opponent")]
        [SerializeField] RectTransform opponentHealthFill;
        [SerializeField] Text opponentHealthText;
        [SerializeField] GameObject opponentGuardIndicator;

        [Header("Messages")]
        [SerializeField] Text outcomeText;
        [SerializeField] float outcomeSeconds = 0.8f;
        [SerializeField] Text resultText;

        float outcomeHideAt;

        void OnEnable()
        {
            gameState.HealthChanged += OnHealthChanged;
            gameState.WeaponChanged += OnWeaponChanged;
            gameState.GuardChanged += OnGuardChanged;
            gameState.PhaseChanged += OnPhaseChanged;
            dispatcher.OutcomeReceived += OnOutcomeReceived;
        }

        void OnDisable()
        {
            gameState.HealthChanged -= OnHealthChanged;
            gameState.WeaponChanged -= OnWeaponChanged;
            gameState.GuardChanged -= OnGuardChanged;
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
            // Your own guard (the hardware guard pose) has no indicator of its own: say when it
            // goes up and when the engine drops it again (3 s, shared/config/match.yaml).
            else if (player == gameState.LocalPlayer)
                ShowMessage(guarding ? "GUARD UP" : "GUARD DOWN", new Color(0.55f, 0.8f, 1f));
        }

        void OnPhaseChanged(MatchPhase phase)
        {
            resultText.gameObject.SetActive(phase == MatchPhase.Ended);
            if (phase == MatchPhase.Ended)
                resultText.text = gameState.Winner == gameState.LocalPlayer ? "ENEMY DEFEATED" : "YOU WERE DEFEATED";
        }

        // ---- Events -----------------------------------------------------------------------

        void OnOutcomeReceived(CombatOutcomeMessage message)
        {
            bool iAttacked = message.attacker == gameState.LocalPlayer;
            string text = message.outcome switch
            {
                Outcome.Hit => iAttacked ? $"HIT  -{message.damage}" : $"YOU WERE HIT  -{message.damage}",
                // A guard blocks all damage (match.yaml block_multiplier 0): show the -0.
                Outcome.Block => iAttacked ? $"BLOCKED  -{message.damage}" : $"GUARDED  -{message.damage}",
                Outcome.Parry => "PARRIED  -0",
                _ => "MISS",
            };
            ShowMessage(text, message.outcome == Outcome.Hit ? new Color(1f, 0.35f, 0.25f) : Color.white);
        }

        void ShowMessage(string text, Color color)
        {
            outcomeText.text = text;
            outcomeText.color = color;
            outcomeText.gameObject.SetActive(true);
            outcomeHideAt = Time.time + outcomeSeconds;
        }

        void Update()
        {
            // Cooldown bars refill continuously, so they are redrawn every frame rather than on an
            // event. They read GameState; they don't change it.
            foreach (var row in cooldownRows)
            {
                float total = gameState.Self.TotalFor(row.action);
                row.overlay.fillAmount = total > 0f ? gameState.Self.RemainingFor(row.action) / total : 0f;
            }

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
