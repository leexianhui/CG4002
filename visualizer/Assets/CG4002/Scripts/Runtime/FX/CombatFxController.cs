using System.Collections;
using System.Collections.Generic;
using CG4002.Combat;
using CG4002.Models;
using UnityEngine;
using UnityEngine.Serialization;

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
        [SerializeField, FormerlySerializedAs("lightTrail")] TrailStyle slashTrail = new() { lifetime = 0.12f, intensity = 1.2f };

        [Header("Hit flash")]
        [SerializeField] float hitFlashSeconds = 0.18f;
        [SerializeField] Color hitFlashColor = new(1f, 0.95f, 0.9f);

        class AvatarFx
        {
            public BladeTrail trail;
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
            avatar.StrikeOpened += OnStrikeOpened;
            avatar.StrikeClosed += OnStrikeClosed;
        }

        public void Unregister(AvatarView avatar)
        {
            if (avatar == null || !active.TryGetValue(avatar, out var fx))
                return;

            avatar.WeaponEquipped -= OnWeaponEquipped;
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

        void OnWeaponEquipped(AvatarView avatar) => active[avatar].trail.Clear();

        void OnStrikeOpened(AvatarView avatar, AttackSlot slot)
        {
            var weapon = avatar.CurrentWeapon;
            if (weapon == null)
                return;
            active[avatar].trail.Begin(weapon.trailBase, weapon.trailTip,
                AccentOf(avatar) * slashTrail.intensity, slashTrail.lifetime);
        }

        void OnStrikeClosed(AvatarView avatar, AttackSlot slot) => active[avatar].trail.End();

        // ---- Effects -----------------------------------------------------------------------

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
