using System;
using CG4002.Combat;
using UnityEngine;

namespace CG4002.Models
{
    /// <summary>
    /// Contract for the opponent avatar. Controllers tell it what to do through the
    /// methods here, and learn when things happen through the events. Parameter and
    /// layer names stay private to this class and the generated assets.
    /// </summary>
    [RequireComponent(typeof(Animator))]
    public class AvatarView : MonoBehaviour
    {
        static readonly int AttackHash = Animator.StringToHash(AvatarContract.AttackTrigger);
        static readonly int AttackSlotHash = Animator.StringToHash(AvatarContract.AttackSlotInt);

        // Clips still fading out during a crossfade also fire events; ignore those.
        const float MinEventWeight = 0.5f;

        [SerializeField] Animator animator;

        [Header("Sockets")]
        public Transform mainHandSocket;
        public Transform offHandSocket;
        [Tooltip("Where hit effects on this avatar spawn.")]
        public Transform hitAnchor;

        [Header("Rendering")]
        [Tooltip("Body renderers, flashed when the avatar takes a hit.")]
        public Renderer[] bodyRenderers;

        [Header("Tuning")]
        [Tooltip("Guard layer weight change per second. 8 means a full raise takes 0.125 s.")]
        [SerializeField] float guardBlendSpeed = 8f;

        float guardWeight;
        bool strikeOpen;

        public Animator Animator => animator;
        public WeaponDefinition CurrentDefinition { get; private set; }
        public WeaponView CurrentWeapon { get; private set; }
        public ShieldView CurrentShield { get; private set; }
        public AttackSlot CurrentSlot { get; private set; }
        public bool IsGuarding { get; private set; }

        public event Action<AvatarView> WeaponEquipped;
        public event Action<AvatarView, AttackSlot> AttackStarted;
        public event Action<AvatarView, AttackSlot> ChargeStarted;
        public event Action<AvatarView, AttackSlot> StrikeOpened;
        public event Action<AvatarView, AttackSlot> StrikeClosed;

        void Reset() => animator = GetComponent<Animator>();

        void Awake()
        {
            if (animator == null)
                animator = GetComponent<Animator>();
        }

        /// <summary>Attach the weapon's models and switch to its animation set.</summary>
        public void Equip(WeaponDefinition definition)
        {
            CloseStrikeIfOpen();
            DestroyHeld(CurrentWeapon);
            DestroyHeld(CurrentShield);

            CurrentDefinition = definition;
            CurrentWeapon = Instantiate(definition.mainHandPrefab, mainHandSocket, false);
            CurrentShield = definition.offHandPrefab != null
                ? Instantiate(definition.offHandPrefab, offHandSocket, false)
                : null;

            // Swapping the controller resets the Animator, so the guard weight is re-applied in Update.
            animator.runtimeAnimatorController = definition.animations;
            WeaponEquipped?.Invoke(this);
        }

        /// <summary>Start an attack. Interrupts any attack already playing (report 6.4.2).</summary>
        public void PlayAttack(AttackSlot slot)
        {
            CloseStrikeIfOpen();
            CurrentSlot = slot;
            animator.SetInteger(AttackSlotHash, (int)slot);
            animator.SetTrigger(AttackHash);
            AttackStarted?.Invoke(this, slot);
        }

        /// <summary>Raise or lower the off-hand guard. Blends over a few frames.</summary>
        public void SetGuard(bool guarding) => IsGuarding = guarding;

        void Update()
        {
            guardWeight = Mathf.MoveTowards(guardWeight, IsGuarding ? 1f : 0f, guardBlendSpeed * Time.deltaTime);
            if (animator.runtimeAnimatorController != null && animator.layerCount > AvatarContract.GuardLayer)
                animator.SetLayerWeight(AvatarContract.GuardLayer, guardWeight);
        }

        void OnDrawGizmosSelected()
        {
            Gizmos.color = Color.green;
            if (mainHandSocket != null) DrawSocket(mainHandSocket);
            if (offHandSocket != null) DrawSocket(offHandSocket);
            Gizmos.color = Color.red;
            if (hitAnchor != null) Gizmos.DrawWireSphere(hitAnchor.position, 0.05f);
        }

        static void DrawSocket(Transform socket)
        {
            Gizmos.DrawWireSphere(socket.position, 0.03f);
            Gizmos.DrawRay(socket.position, socket.up * 0.15f);  // direction held items extend
        }

        void CloseStrikeIfOpen()
        {
            if (!strikeOpen)
                return;
            strikeOpen = false;
            StrikeClosed?.Invoke(this, CurrentSlot);
        }

        static void DestroyHeld(HeldItemView item)
        {
            if (item == null)
                return;
            if (Application.isPlaying)
                Destroy(item.gameObject);
            else
                DestroyImmediate(item.gameObject);
        }

        // ---- Animation Event receivers ------------------------------------------------
        // Unity calls these by name from events baked into the clips, and only looks for
        // them on components next to the Animator. The names must match AvatarContract.

        void ChargeStart(AnimationEvent e)
        {
            if (e.animatorClipInfo.weight < MinEventWeight)
                return;
            ChargeStarted?.Invoke(this, CurrentSlot);
        }

        void StrikeOpen(AnimationEvent e)
        {
            if (e.animatorClipInfo.weight < MinEventWeight)
                return;
            strikeOpen = true;
            StrikeOpened?.Invoke(this, CurrentSlot);
        }

        void StrikeClose(AnimationEvent e)
        {
            if (e.animatorClipInfo.weight < MinEventWeight)
                return;
            CloseStrikeIfOpen();
        }
    }
}
