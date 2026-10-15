using CG4002.Combat;
using CG4002.FX;
using CG4002.Models;
using CG4002.State;
using UnityEngine;

namespace CG4002.Controllers
{
    /// <summary>
    /// Makes the avatar show one player: listens to GameState for what the player *is*
    /// (weapon, guard, position) and to the dispatcher for what they *do* (attacks, being hit),
    /// and turns both into AvatarView calls. AvatarView knows how to animate a body; this
    /// knows which player the body represents and what the messages mean.
    ///
    /// Put it on the avatar. Its parent is the arena: arena coordinates are local positions.
    /// </summary>
    public class AvatarController : MonoBehaviour
    {
        [SerializeField] AvatarView avatar;
        [SerializeField] CombatFxController fx;
        [SerializeField] WeaponCatalog catalog;
        [SerializeField] GameState gameState;
        [SerializeField] MessageDispatcher dispatcher;

        [Tooltip("The player this avatar represents. P2 is the dummy/opponent.")]
        [SerializeField] PlayerId represents = PlayerId.P2;

        [Tooltip("How quickly the avatar catches up with a new position (report 6.4.3). Higher is snappier.")]
        [SerializeField] float followSpeed = 8f;

        Vector3 targetPosition;
        Quaternion targetRotation;

        public void ConfigurePlayer(PlayerId player) => represents = player;

        void Awake()
        {
            targetPosition = transform.localPosition;
            targetRotation = transform.localRotation;
        }

        void OnEnable()
        {
            gameState.WeaponChanged += OnWeaponChanged;
            gameState.GuardChanged += OnGuardChanged;
            gameState.PositionChanged += OnPositionChanged;
            dispatcher.ActionReceived += OnActionReceived;
            dispatcher.OutcomeReceived += OnOutcomeReceived;
            // Online state may arrive while the arena is hidden for placement.
            var current = gameState.Get(represents);
            if (current.weaponInitialized) OnWeaponChanged(represents, current.weapon);
            OnGuardChanged(represents, current.guarding);
            if (current.hasPosition) OnPositionChanged(represents, current.position, current.headingDegrees);
        }

        void OnDisable()
        {
            gameState.WeaponChanged -= OnWeaponChanged;
            gameState.GuardChanged -= OnGuardChanged;
            gameState.PositionChanged -= OnPositionChanged;
            dispatcher.ActionReceived -= OnActionReceived;
            dispatcher.OutcomeReceived -= OnOutcomeReceived;
        }

        void OnWeaponChanged(PlayerId player, WeaponType weapon)
        {
            if (player == represents)
            {
                if (gameState.Get(player).weaponSelected) avatar.Equip(catalog.Get(weapon));
                else
                {
                    if (avatar.CurrentWeapon != null) avatar.CurrentWeapon.gameObject.SetActive(false);
                    if (avatar.CurrentShield != null) avatar.CurrentShield.gameObject.SetActive(false);
                }
            }
        }

        void OnGuardChanged(PlayerId player, bool guarding)
        {
            if (player == represents)
                avatar.SetGuard(guarding);
        }

        void OnPositionChanged(PlayerId player, Vector3 position, float headingDegrees)
        {
            if (player != represents)
                return;
            // Only set a target; Update moves towards it, so updates arriving at network rate
            // don't make the avatar jump between spots.
            targetPosition = position;
            targetRotation = Quaternion.Euler(0f, headingDegrees, 0f);
        }

        void OnActionReceived(ActionMessage message)
        {
            if (message.player != represents || avatar.CurrentDefinition == null)
                return;
            if (avatar.CurrentDefinition.TryGetSlot(message.action, out AttackSlot slot))
                avatar.PlayAttack(slot);
        }

        void OnOutcomeReceived(CombatOutcomeMessage message)
        {
            // message.player is the defender: react when this avatar is the one being attacked.
            if (message.player != represents)
                return;
            if (message.outcome == Outcome.Hit)
                fx.PlayHit(avatar);
            else if (message.outcome == Outcome.Block)
                fx.PlayBlock(avatar);
        }

        void Update()
        {
            float t = 1f - Mathf.Exp(-followSpeed * Time.deltaTime);
            transform.localPosition = Vector3.Lerp(transform.localPosition, targetPosition, t);
            transform.localRotation = Quaternion.Slerp(transform.localRotation, targetRotation, t);
        }
    }
}
