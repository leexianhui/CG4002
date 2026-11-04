using CG4002.AR;
using CG4002.Combat;
using CG4002.FX;
using CG4002.Models;
using CG4002.State;
using UnityEngine;
using UnityEngine.UI;

namespace CG4002.Controllers
{
    /// <summary>
    /// The local player's own attacks, seen first-person. When the AI recognises one of this
    /// player's attacks (ActionRecognizedMessage), a copy of the avatar locked to the camera plays
    /// that move's clip, drawing only the arms holding the weapon, and the move's name flashes.
    /// With the sword that's the sword arm alone: the shield sits across the chest in the ready
    /// stance, right in front of the camera, and would hide most of the view. Two-handed weapons
    /// (spear, mace) show both forearms on the shaft.
    ///
    /// It confirms the move rather than mirroring it live: recognition arrives after the real
    /// swing has finished. Image tracking carries idle and guard; during the animation the
    /// tracked prop sword is hidden (tracking itself keeps running) so there are never two swords.
    /// Moves still on cooldown don't play, so the cooldown is felt.
    /// </summary>
    public class FirstPersonAttack : MonoBehaviour
    {
        [SerializeField] MessageDispatcher dispatcher;
        [SerializeField] GameState gameState;
        [SerializeField] WeaponCatalog catalog;
        [SerializeField] AvatarView avatarPrefab;
        [SerializeField] Camera arCamera;
        [Tooltip("Optional. Its sword is hidden while the animation plays.")]
        [SerializeField] PropTracker prop;
        [Tooltip("Optional. Gives the first-person strikes slash trails and the charge glow.")]
        [SerializeField] CombatFxController fx;
        [Tooltip("Optional. Shows the recognised move's name.")]
        [SerializeField] Text moveLabel;

        [Header("Placement")]
        // Like a first-person game's viewmodel, the avatar sits higher and further forward than a
        // real body would (its eyes are at 1.62 m), so the strikes pass through the middle of the view.
        [Tooltip("Camera height above the avatar's feet. Lower raises the arm in view.")]
        [SerializeField] float eyeHeight = 1.38f;
        [Tooltip("How far ahead of the camera the avatar stands, moving the arm further from the lens.")]
        [SerializeField] float forward = 0.3f;
        [SerializeField] float labelSeconds = 0.9f;

        const string WeaponForearm = "Forearm_R";
        const string OffForearm = "Forearm_L";

        AvatarView avatar;
        bool playing;
        float playingFor;
        float labelHideAt;

        public bool IsPlaying => playing;
        public AvatarView Avatar => avatar;

        void Awake()
        {
            avatar = Instantiate(avatarPrefab, arCamera.transform, false);
            avatar.name = "First Person Avatar";
            avatar.transform.SetLocalPositionAndRotation(new Vector3(0f, -eyeHeight, forward), Quaternion.identity);
            if (fx != null)
                fx.Register(avatar);
            SetVisible(false);
            if (moveLabel != null)
                moveLabel.enabled = false;
        }

        void OnEnable() => dispatcher.ActionRecognized += OnActionRecognized;
        void OnDisable() => dispatcher.ActionRecognized -= OnActionRecognized;

        void OnDestroy()
        {
            if (fx != null && avatar != null)
                fx.Unregister(avatar);
        }

        void OnActionRecognized(ActionRecognizedMessage message)
        {
            if (message.player != gameState.LocalPlayer || message.onCooldown)
                return;

            var definition = catalog.Get(gameState.Self.weapon);
            if (definition == null || !definition.TryGetSlot(message.action, out AttackSlot slot))
                return;
            if (avatar.CurrentDefinition != definition)
                avatar.Equip(definition);

            SetVisible(true);
            avatar.PlayAttack(slot);
            playing = true;
            playingFor = 0f;
            if (prop != null)
                prop.Suppressed = true;
            ShowLabel(message.action);
        }

        void Update()
        {
            if (playing)
            {
                playingFor += Time.deltaTime;
                // The Animator enters the attack state on its next update; once it's back in Idle, the move is over.
                var state = avatar.Animator.GetCurrentAnimatorStateInfo(AvatarContract.BodyLayer);
                bool backToIdle = state.IsName(AvatarContract.IdleState) && !avatar.Animator.IsInTransition(AvatarContract.BodyLayer);
                if (playingFor > 0.15f && backToIdle)
                    Stop();
            }
            if (moveLabel != null && moveLabel.enabled && Time.time >= labelHideAt)
                moveLabel.enabled = false;
        }

        void Stop()
        {
            playing = false;
            SetVisible(false);
            if (prop != null)
                prop.Suppressed = false;
        }

        /// <summary>Draw only the arms holding the weapon: their forearms, hands and the weapon.</summary>
        void SetVisible(bool visible)
        {
            bool twoHanded = avatar.CurrentDefinition != null && avatar.CurrentDefinition.twoHanded;
            foreach (var renderer in avatar.GetComponentsInChildren<Renderer>(true))
                renderer.enabled = visible && HoldsWeapon(renderer.transform, twoHanded);
        }

        static bool HoldsWeapon(Transform part, bool twoHanded)
        {
            for (var t = part; t != null; t = t.parent)
            {
                if (t.name == WeaponForearm || (twoHanded && t.name == OffForearm))
                    return true;
            }
            return false;
        }

        void ShowLabel(ActionType action)
        {
            if (moveLabel == null)
                return;
            moveLabel.text = SpacedUpper(action.ToString());
            moveLabel.enabled = true;
            labelHideAt = Time.time + labelSeconds;
        }

        /// <summary>"HorizontalSwing" → "HORIZONTAL SWING".</summary>
        static string SpacedUpper(string name)
        {
            var text = new System.Text.StringBuilder();
            for (int i = 0; i < name.Length; i++)
            {
                if (i > 0 && char.IsUpper(name[i]))
                    text.Append(' ');
                text.Append(char.ToUpperInvariant(name[i]));
            }
            return text.ToString();
        }
    }
}
