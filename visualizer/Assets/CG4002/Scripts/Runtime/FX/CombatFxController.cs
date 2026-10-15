using System.Collections;
using System.Collections.Generic;
using CG4002.Combat;
using CG4002.Models;
using UnityEngine;

namespace CG4002.FX
{
    /// <summary>
    /// Plays combat effects for one or more avatars. It reacts to AvatarView events and
    /// reads anchors from the equipped weapon, so it never needs to know which weapon
    /// model is attached or when in a clip the blade starts moving.
    /// </summary>
    public class CombatFxController : MonoBehaviour
    {
        static readonly int EmissionId = Shader.PropertyToID("_EmissionColor");
        static readonly int BaseColorId = Shader.PropertyToID("_BaseColor");

        [System.Serializable]
        public struct TrailStyle
        {
            [Tooltip("Seconds each part of the trail stays visible.")]
            public float lifetime;
            [Tooltip("Multiplier on the weapon accent colour. Above 1 blooms.")]
            public float intensity;
        }

        [SerializeField] List<AvatarView> avatars = new();

        [Header("Assets")]
        [SerializeField] Material trailMaterial;
        [SerializeField] ParticleSystem impactBurstPrefab;

        [Header("Slash trails")]
        [SerializeField] TrailStyle lightTrail = new() { lifetime = 0.12f, intensity = 1.2f };
        [SerializeField] TrailStyle chargedTrail = new() { lifetime = 0.28f, intensity = 2.2f };

        [Header("Charge glow")]
        [SerializeField] float chargeRampSeconds = 0.3f;
        [SerializeField] float chargePeakIntensity = 4f;
        [SerializeField] float glowFadeSeconds = 0.35f;

        [Header("Hit flash")]
        [SerializeField] float hitFlashSeconds = 0.18f;
        [SerializeField] Color hitFlashColor = new(1f, 0.95f, 0.9f);

        class AvatarFx
        {
            public BladeTrail trail;
            public Coroutine glow;
            public Coroutine flash;
        }

        readonly Dictionary<AvatarView, AvatarFx> active = new();
        MaterialPropertyBlock block;

        void Awake() => block = new MaterialPropertyBlock();

        void OnEnable()
        {
            foreach (var avatar in avatars)
                Register(avatar);
        }

        void OnDisable()
        {
            foreach (var avatar in new List<AvatarView>(active.Keys))
                Unregister(avatar);
        }

        public void Register(AvatarView avatar)
        {
            if (avatar == null || active.ContainsKey(avatar))
                return;

            var trailObject = new GameObject($"{avatar.name} Trail", typeof(MeshFilter), typeof(MeshRenderer), typeof(BladeTrail));
            trailObject.GetComponent<MeshRenderer>().sharedMaterial = trailMaterial;
            active[avatar] = new AvatarFx { trail = trailObject.GetComponent<BladeTrail>() };

            avatar.WeaponEquipped += OnWeaponEquipped;
            avatar.AttackStarted += OnAttackStarted;
            avatar.ChargeStarted += OnChargeStarted;
            avatar.StrikeOpened += OnStrikeOpened;
            avatar.StrikeClosed += OnStrikeClosed;
        }

        public void Unregister(AvatarView avatar)
        {
            if (avatar == null || !active.TryGetValue(avatar, out var fx))
                return;

            avatar.WeaponEquipped -= OnWeaponEquipped;
            avatar.AttackStarted -= OnAttackStarted;
            avatar.ChargeStarted -= OnChargeStarted;
            avatar.StrikeOpened -= OnStrikeOpened;
            avatar.StrikeClosed -= OnStrikeClosed;

            if (fx.trail != null)
                Destroy(fx.trail.gameObject);
            active.Remove(avatar);
        }

        /// <summary>
        /// Hit reaction on an avatar: called by AvatarController for a HIT outcome.
        /// </summary>
        public void PlayHit(AvatarView avatar)
        {
            if (!active.TryGetValue(avatar, out var fx))
                return;
            if (fx.flash != null)
                StopCoroutine(fx.flash);
            fx.flash = StartCoroutine(HitFlash(avatar));
            SpawnBurst(avatar.hitAnchor, hitFlashColor);
        }

        /// <summary>
        /// Blocked attack on an avatar: a white burst off the shield, and no body flash, so it
        /// reads differently from a hit.
        /// </summary>
        public void PlayBlock(AvatarView avatar)
        {
            var shield = avatar.CurrentShield;
            SpawnBurst(shield != null ? shield.blockFace : avatar.hitAnchor, Color.white);
        }

        // ---- AvatarView event handlers ---------------------------------------------------

        void OnWeaponEquipped(AvatarView avatar)
        {
            var fx = active[avatar];
            fx.trail.Clear();
            StopGlow(avatar, fx);
        }

        void OnAttackStarted(AvatarView avatar, AttackSlot slot)
        {
            // A new attack interrupts any glow left over from the previous one.
            StopGlow(avatar, active[avatar]);
        }

        void OnChargeStarted(AvatarView avatar, AttackSlot slot)
        {
            var fx = active[avatar];
            StopGlow(avatar, fx);
            fx.glow = StartCoroutine(ChargeGlow(avatar));
        }

        void OnStrikeOpened(AvatarView avatar, AttackSlot slot)
        {
            var weapon = avatar.CurrentWeapon;
            if (weapon == null)
                return;

            var style = slot == AttackSlot.Charged ? chargedTrail : lightTrail;
            Color accent = AccentOf(avatar);
            active[avatar].trail.Begin(weapon.trailBase, weapon.trailTip, accent * style.intensity, style.lifetime);

            if (slot == AttackSlot.Charged)
                SpawnBurst(weapon.strikePoint, accent);
        }

        void OnStrikeClosed(AvatarView avatar, AttackSlot slot)
        {
            var fx = active[avatar];
            fx.trail.End();
            if (fx.glow != null)
            {
                StopCoroutine(fx.glow);
                fx.glow = StartCoroutine(FadeGlow(avatar, chargePeakIntensity));
            }
        }

        // ---- Effects -----------------------------------------------------------------------

        IEnumerator ChargeGlow(AvatarView avatar)
        {
            float t = 0f;
            while (true)
            {
                t += Time.deltaTime;
                float ramp = Mathf.Clamp01(t / chargeRampSeconds);
                float flicker = 1f + 0.15f * Mathf.Sin(t * 60f) * ramp;
                SetGlow(avatar, Mathf.Lerp(1f, chargePeakIntensity, ramp * ramp) * flicker);

                // Safety net: if StrikeClose never arrives, don't glow forever.
                if (t > chargeRampSeconds + 2f)
                    break;
                yield return null;
            }
            yield return FadeGlow(avatar, chargePeakIntensity);
        }

        IEnumerator FadeGlow(AvatarView avatar, float from)
        {
            for (float t = 0f; t < glowFadeSeconds; t += Time.deltaTime)
            {
                SetGlow(avatar, Mathf.Lerp(from, 1f, t / glowFadeSeconds));
                yield return null;
            }
            ClearGlow(avatar);
            active[avatar].glow = null;
        }

        void StopGlow(AvatarView avatar, AvatarFx fx)
        {
            if (fx.glow != null)
                StopCoroutine(fx.glow);
            fx.glow = null;
            ClearGlow(avatar);
        }

        void SetGlow(AvatarView avatar, float intensity)
        {
            block.Clear();
            block.SetColor(EmissionId, AccentOf(avatar) * intensity);
            ForEachGlowRenderer(avatar, r => r.SetPropertyBlock(block));
        }

        void ClearGlow(AvatarView avatar) => ForEachGlowRenderer(avatar, r => r.SetPropertyBlock(null));

        static void ForEachGlowRenderer(AvatarView avatar, System.Action<Renderer> action)
        {
            HeldItemView[] items = { avatar.CurrentWeapon, avatar.CurrentShield };
            foreach (var item in items)
            {
                if (item == null || item.glowRenderers == null)
                    continue;
                foreach (var r in item.glowRenderers)
                {
                    if (r != null)
                        action(r);
                }
            }
        }

        IEnumerator HitFlash(AvatarView avatar)
        {
            for (float t = 0f; t < hitFlashSeconds; t += Time.deltaTime)
            {
                float k = 1f - t / hitFlashSeconds;
                ApplyFlash(avatar, k * k);
                yield return null;
            }
            foreach (var r in avatar.bodyRenderers)
            {
                for (int m = 0; m < r.sharedMaterials.Length; m++)
                    r.SetPropertyBlock(null, m);
            }
            active[avatar].flash = null;
        }

        void ApplyFlash(AvatarView avatar, float amount)
        {
            foreach (var r in avatar.bodyRenderers)
            {
                var materials = r.sharedMaterials;
                for (int m = 0; m < materials.Length; m++)
                {
                    Color baseColor = materials[m].GetColor(BaseColorId);
                    Color emission = materials[m].GetColor(EmissionId);
                    block.Clear();
                    block.SetColor(BaseColorId, Color.Lerp(baseColor, hitFlashColor, amount));
                    block.SetColor(EmissionId, Color.Lerp(emission, hitFlashColor * 2f, amount));
                    r.SetPropertyBlock(block, m);
                }
            }
        }

        void SpawnBurst(Transform anchor, Color color)
        {
            if (impactBurstPrefab == null || anchor == null)
                return;
            var burst = Instantiate(impactBurstPrefab, anchor.position, anchor.rotation);
            var main = burst.main;
            float max = Mathf.Max(color.maxColorComponent, 1f);
            main.startColor = new Color(color.r / max, color.g / max, color.b / max, 1f);
            burst.Play(true);
        }

        static Color AccentOf(AvatarView avatar) =>
            avatar.CurrentDefinition != null ? avatar.CurrentDefinition.accentColor : Color.white;
    }
}
