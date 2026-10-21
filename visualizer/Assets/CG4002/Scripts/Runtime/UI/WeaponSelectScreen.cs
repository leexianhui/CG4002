using System.Collections.Generic;
using CG4002.AR;
using CG4002.Combat;
using CG4002.Models;
using CG4002.State;
using UnityEngine;
using UnityEngine.UI;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace CG4002.UI
{
    /// <summary>
    /// Weapon selection, before the match. While the engine reports that this player has no weapon
    /// (the wearable hub hasn't locked one), one hologram dummy per offered weapon stands in front
    /// of the camera in that weapon's ready stance, mirrored so the player copies it like a mirror.
    ///
    /// The hub decides: holding a stance for 3 s locks it, and the engine reports the weapon. This
    /// screen never picks or sends one. When the lock arrives the chosen dummy flashes, the screen
    /// closes, and arena placement (calibration) starts. The HUD and placement are held off until then.
    ///
    /// Opens only on an explicit "no weapon" from the engine, so it stays closed offline (unless
    /// LocalGameRules requires selection) and before the first snapshot.
    /// </summary>
    public class WeaponSelectScreen : MonoBehaviour
    {
        enum Stage { Closed, Choosing, Confirming }

        static readonly int ColorId = Shader.PropertyToID("_Color");
        const float ModelHeight = 1.8f;
        const float FallbackEyeHeight = 1.4f;

        [SerializeField] GameState gameState;
        [SerializeField] WeaponCatalog catalog;
        [SerializeField] AvatarView dummyPrefab;
        [SerializeField] Material hologram;
        [SerializeField] Camera arCamera;
        [Tooltip("Optional. The lowest detected floor plane sets the dummies' feet; otherwise 1.4 m below the camera.")]
        [SerializeField] ARPlaneManager planeManager;
        [Tooltip("Optional. Held off (and reset) while choosing, so calibration comes after selection.")]
        [SerializeField] DummyPlacer placer;
        [Tooltip("Optional. The game HUD, hidden while choosing.")]
        [SerializeField] Canvas hud;
        [SerializeField] Text title;
        [SerializeField] Text instruction;

        [Header("Layout")]
        [Tooltip("Weapons offered, left to right as the player sees them.")]
        [SerializeField] WeaponType[] weapons = { WeaponType.SwordAndShield };
        [SerializeField] float distance = 2.5f;
        [SerializeField] float spacing = 1.2f;
        [SerializeField] float dummyHeight = 1.6f;
        [Tooltip("Seconds the dummies may be out of view before they move back in front of the camera.")]
        [SerializeField] float recentreAfter = 1.5f;

        [Header("Timing")]
        [Tooltip("Shown to the player; the hub's WEAPON_LOCK_HOLD_MS decides.")]
        [SerializeField] float holdSeconds = 3f;
        [SerializeField] float confirmSeconds = 1.5f;

        readonly Dictionary<WeaponType, AvatarView> dummies = new();
        readonly List<TextMesh> labels = new();
        Transform stage;
        Stage current = Stage.Closed;
        float stageTimer;
        float outOfViewTime;
        WeaponType chosen;

        public bool IsSelecting => current != Stage.Closed;

        bool NeedsWeapon => gameState.Self.weaponInitialized && !gameState.Self.weaponSelected;

        void Start() => SetText("", "");

        void Update()
        {
            switch (current)
            {
                case Stage.Closed:
                    if (NeedsWeapon)
                        Open();
                    else
                        ShowOpponentStatus();
                    break;
                case Stage.Choosing:
                    if (!NeedsWeapon)
                        Confirm(gameState.Self.weapon);
                    else
                        KeepInView();
                    break;
                case Stage.Confirming:
                    stageTimer += Time.deltaTime;
                    Flash(stageTimer / confirmSeconds);
                    if (NeedsWeapon)
                        Open();  // the hub dropped the lock (e.g. rebooted): choose again
                    else if (stageTimer >= confirmSeconds)
                        Close();
                    break;
            }
            FaceLabelsToCamera();
        }

        void Open()
        {
            Clear();
            current = Stage.Choosing;
            if (placer != null)
            {
                placer.BeginPlacement();
                placer.enabled = false;
            }
            if (hud != null)
                hud.enabled = false;

            stage = new GameObject("Weapon Select Stage").transform;
            for (int i = 0; i < weapons.Length; i++)
                SpawnDummy(weapons[i], i);
            PlaceInFrontOfCamera();

            SetText(weapons.Length == 1 ? "COPY THIS POSE" : "CHOOSE YOUR WEAPON",
                $"Hold the pose for {holdSeconds:0} seconds");
        }

        void Confirm(WeaponType weapon)
        {
            current = Stage.Confirming;
            stageTimer = 0f;
            chosen = weapon;
            foreach (var (type, dummy) in dummies)
                dummy.gameObject.SetActive(type == weapon);
            var definition = catalog.Get(weapon);
            SetText($"{(definition != null ? definition.displayName : weapon.ToString()).ToUpperInvariant()} SELECTED", "");
        }

        void Close()
        {
            Clear();
            current = Stage.Closed;
            if (placer != null)
                placer.enabled = true;
            if (hud != null)
                hud.enabled = true;
            SetText("", "");
        }

        void Clear()
        {
            if (stage != null)
                Destroy(stage.gameObject);
            stage = null;
            dummies.Clear();
            labels.Clear();
            outOfViewTime = 0f;
        }

        void SpawnDummy(WeaponType weapon, int index)
        {
            var definition = catalog.Get(weapon);
            if (definition == null)
                return;

            // The stage faces the camera, so its -X is the player's right: lay slots out from the player's left.
            float x = ((weapons.Length - 1) * 0.5f - index) * spacing;
            var slot = new GameObject(weapon.ToString()).transform;
            slot.SetParent(stage, false);
            slot.localPosition = new Vector3(x, 0f, 0f);

            var dummy = Instantiate(dummyPrefab, slot, false);
            dummy.Equip(definition);
            float scale = dummyHeight / ModelHeight;
            // Mirrored: the dummy's weapon hand appears on the same side as the player's.
            dummy.transform.localScale = new Vector3(-scale, scale, scale);
            MakeHologram(dummy, definition.accentColor);
            dummies[weapon] = dummy;

            labels.Add(Label(slot, definition.displayName.ToUpperInvariant(), definition.accentColor, dummyHeight + 0.25f));
        }

        void MakeHologram(AvatarView dummy, Color colour)
        {
            foreach (var renderer in dummy.GetComponentsInChildren<Renderer>(true))
            {
                var materials = new Material[renderer.sharedMaterials.Length];
                for (int i = 0; i < materials.Length; i++)
                    materials[i] = hologram;
                renderer.sharedMaterials = materials;
                renderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            }
            SetColour(dummy, colour);
        }

        static void SetColour(AvatarView dummy, Color colour)
        {
            var block = new MaterialPropertyBlock();
            block.SetColor(ColorId, colour);
            foreach (var renderer in dummy.GetComponentsInChildren<Renderer>(true))
                renderer.SetPropertyBlock(block);
        }

        static TextMesh Label(Transform parent, string text, Color colour, float height)
        {
            var label = new GameObject("Label").AddComponent<TextMesh>();
            label.transform.SetParent(parent, false);
            label.transform.localPosition = new Vector3(0f, height, 0f);
            label.text = text;
            label.font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
            label.GetComponent<MeshRenderer>().sharedMaterial = label.font.material;
            label.fontSize = 64;
            label.characterSize = 0.02f;
            label.anchor = TextAnchor.MiddleCenter;
            label.fontStyle = FontStyle.Bold;
            label.color = colour.maxColorComponent > 1f ? colour / colour.maxColorComponent : colour;
            return label;
        }

        /// <summary>Stand the dummies on the floor, `distance` ahead of the camera, facing it.</summary>
        void PlaceInFrontOfCamera()
        {
            Transform view = arCamera.transform;
            Vector3 forward = Vector3.ProjectOnPlane(view.forward, Vector3.up);
            if (forward.sqrMagnitude < 1e-4f)
                forward = Vector3.ProjectOnPlane(view.up, Vector3.up);  // looking straight down
            forward.Normalize();

            Vector3 centre = view.position + forward * distance;
            centre.y = FloorHeight();
            stage.SetPositionAndRotation(centre, Quaternion.LookRotation(-forward, Vector3.up));
            outOfViewTime = 0f;
        }

        float FloorHeight()
        {
            float fallback = arCamera.transform.position.y - FallbackEyeHeight;
            if (planeManager == null)
                return fallback;
            float lowest = float.PositiveInfinity;
            foreach (var plane in planeManager.trackables)
            {
                if (plane.alignment == PlaneAlignment.HorizontalUp && plane.transform.position.y < arCamera.transform.position.y)
                    lowest = Mathf.Min(lowest, plane.transform.position.y);
            }
            return float.IsPositiveInfinity(lowest) ? fallback : lowest;
        }

        /// <summary>If the player has turned away for a while, bring the dummies back in front of them.</summary>
        void KeepInView()
        {
            Vector3 viewport = arCamera.WorldToViewportPoint(stage.position + Vector3.up * dummyHeight * 0.5f);
            bool visible = viewport.z > 0f && viewport.x > -0.1f && viewport.x < 1.1f && viewport.y > -0.1f && viewport.y < 1.1f;
            outOfViewTime = visible ? 0f : outOfViewTime + Time.deltaTime;
            if (outOfViewTime > recentreAfter)
                PlaceInFrontOfCamera();
        }

        /// <summary>Pulse the chosen dummy brighter, fading out over the confirmation.</summary>
        void Flash(float progress)
        {
            if (!dummies.TryGetValue(chosen, out var dummy))
                return;
            var definition = catalog.Get(chosen);
            float boost = 1f + 2.5f * Mathf.Abs(Mathf.Sin(progress * Mathf.PI * 3f)) * (1f - Mathf.Clamp01(progress));
            SetColour(dummy, definition.accentColor * boost);
        }

        void FaceLabelsToCamera()
        {
            foreach (var label in labels)
            {
                if (label != null)
                    label.transform.rotation = Quaternion.LookRotation(label.transform.position - arCamera.transform.position);
            }
        }

        /// <summary>Once this player has a weapon, say so if the opponent is still choosing.</summary>
        void ShowOpponentStatus()
        {
            var opponent = gameState.Opponent;
            bool waiting = gameState.Self.weaponSelected && opponent.weaponInitialized && !opponent.weaponSelected;
            SetText("", waiting ? "Waiting for your opponent to choose a weapon" : "");
        }

        void SetText(string heading, string detail)
        {
            if (title != null)
            {
                title.text = heading;
                title.enabled = heading.Length > 0;
            }
            if (instruction != null)
            {
                instruction.text = detail;
                instruction.enabled = detail.Length > 0;
            }
        }
    }
}
