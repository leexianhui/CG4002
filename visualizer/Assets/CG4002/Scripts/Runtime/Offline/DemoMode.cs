using System;
using CG4002.AR;
using CG4002.Combat;
using CG4002.State;
using CG4002.UI;
using UnityEngine;
using UnityEngine.UI;

namespace CG4002.Offline
{
    /// <summary>
    /// Demo mode (Docs/DemoMode.md): the whole game on one phone with no hardware and no engine. The
    /// online panel's DEMO MODE button calls Begin, which switches the demo engine (LocalGameRules) on
    /// in place of the online client. The weapons are picked with buttons on the selection screen
    /// (yours, then the enemy's), the enemy is placed with a tap as usual, and this control bar
    /// stands in for both players' wearables:
    ///
    ///   [ YOU | ENEMY ] [ CHANGE WEAPONS ]
    ///   [6 attacks, with cooldown sweeps] [ GUARD (hold) ]
    ///
    /// The switch says whose attacks and guard the bar plays. Like MockDataInjector it calls the demo
    /// engine directly, since the wearables are on the engine's side of the boundary. The enemy only
    /// acts through these buttons. The attack row sits where the HUD's cooldown icons are and shows
    /// the same sweeps, so those icons are hidden while demo mode is on.
    /// </summary>
    public class DemoMode : MonoBehaviour
    {
        [Serializable]
        public class AttackButton
        {
            public ActionType action;
            public Button button;
            public Image overlay;
        }

        [SerializeField] GameState gameState;
        [SerializeField] LocalGameRules rules;
        [SerializeField] DummyPlacer placer;
        [SerializeField] WeaponSelectScreen weaponSelect;
        [Tooltip("The HUD's own cooldown icons: the attack row replaces them in demo mode.")]
        [SerializeField] GameObject hudCooldowns;

        [Header("Control bar")]
        [SerializeField] GameObject bar;
        [SerializeField] Button youButton;
        [SerializeField] Button enemyButton;
        [SerializeField] Button changeWeaponsButton;
        [SerializeField] AttackButton[] attacks = Array.Empty<AttackButton>();
        [SerializeField] HoldButton guardButton;

        static readonly Color Selected = Color.white;
        static readonly Color Unselected = new(0.45f, 0.45f, 0.45f, 0.85f);

        bool controllingEnemy;
        PlayerId? guarding;

        public bool Active { get; private set; }

        PlayerId Controlled => controllingEnemy ? gameState.OpponentPlayer : gameState.LocalPlayer;

        void Awake()
        {
            bar.SetActive(false);
            youButton.onClick.AddListener(() => Control(enemy: false));
            enemyButton.onClick.AddListener(() => Control(enemy: true));
            changeWeaponsButton.onClick.AddListener(ChangeWeapons);
            foreach (var attack in attacks)
            {
                var action = attack.action;
                attack.button.onClick.AddListener(() => rules.Attack(Controlled, action));
            }
            guardButton.HeldChanged += SetGuard;
            Control(enemy: false);
        }

        /// <summary>
        /// Switches to demo mode: the demo engine on (with no automatic enemy behaviour), both weapons
        /// to choose. The caller (the online client) disconnects and turns itself off.
        /// </summary>
        public void Begin()
        {
            if (Active)
                return;
            Active = true;
            // Before the engine object wakes up, so the editor sandbox's enemy routines and prop
            // contact never start: everything comes from the buttons.
            foreach (var component in new MonoBehaviour[] { rules.GetComponent<MockDataInjector>(), rules.GetComponent<SwordHitDetector>() })
            {
                if (component != null)
                    component.enabled = false;
            }
            rules.gameObject.SetActive(true);
            rules.enabled = true;
            weaponSelect.PickWeapon = rules.LockWeapon;
            rules.BeginWeaponSelection();
            if (hudCooldowns != null)
                hudCooldowns.SetActive(false);
        }

        void Update()
        {
            // Only once both weapons are chosen and the enemy is placed.
            bool show = Active && placer.IsPlaced && !weaponSelect.IsSelecting;
            if (bar.activeSelf != show)
                bar.SetActive(show);
            if (!show)
                return;

            var player = gameState.Get(Controlled);
            foreach (var attack in attacks)
            {
                float total = player.TotalFor(attack.action);
                attack.overlay.fillAmount = total > 0f ? player.RemainingFor(attack.action) / total : 0f;
            }
        }

        void Control(bool enemy)
        {
            if (guardButton.Held)
                SetGuard(false);  // the guard belongs to the side that raised it
            controllingEnemy = enemy;
            youButton.image.color = enemy ? Unselected : Selected;
            enemyButton.image.color = enemy ? Selected : Unselected;
            if (guardButton.Held)
                SetGuard(true);
        }

        void SetGuard(bool held)
        {
            if (guarding is PlayerId previous)
                rules.SetGuard(previous, false);
            guarding = held ? Controlled : null;
            if (held)
                rules.SetGuard(Controlled, true);
        }

        void ChangeWeapons()
        {
            SetGuard(false);
            rules.UnlockWeapons();  // the selection screen reopens; placing the enemy starts a new match
        }
    }
}
