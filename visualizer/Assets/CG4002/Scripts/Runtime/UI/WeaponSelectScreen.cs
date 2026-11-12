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
    /// screen never picks or sends one. When the lock arrives the chosen dummy flashes and the screen
    /// closes. The HUD and the opponent avatar are hidden until then.
    ///
    /// Opens only on an explicit "no weapon" from the engine, so it stays closed offline (unless
    /// LocalGameRules requires selection) and before the first snapshot. It also waits until the
    /// arena is placed and (online) calibrated: the one tap is done with the phone in hand, then the
    /// headset goes on, the view splits, and the rest needs no touch. Until then it shows what to do
    /// for calibration (look at the opponent and hold still), and later if AR tracking is lost.
    ///
    /// Demo mode (no hub): a button under each dummy picks instead of the pose, first for you, then
    /// for the enemy (DemoMode sets PickWeapon). It chooses first and places after, as before.
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
        [Tooltip("Optional. Selection waits for placement; the opponent avatar is hidden while choosing.")]
        [SerializeField] DummyPlacer placer;
        [Tooltip("Optional (OnlineScene). Selection waits for calibration; its hints show here meanwhile.")]
        [SerializeField] CG4002.Net.SharedArenaCalibration calibration;
        [Tooltip("Optional. The game HUD, hidden while choosing.")]
        [SerializeField] Canvas hud;
        [SerializeField] Text title;
        [SerializeField] Text instruction;
        [Tooltip("Font for the weapon names above the dummies (the HUD's). Unity's built-in font if empty.")]
        [SerializeField] Font labelFont;
        [Tooltip("Demo mode: one per offered weapon (same order), shown under its dummy.")]
        [SerializeField] Button[] pickButtons = System.Array.Empty<Button>();

        [Header("Layout")]
        [Tooltip("Weapons offered, left to right as the player sees them.")]
        [SerializeField] WeaponType[] weapons = { WeaponType.Spear, WeaponType.SwordAndShield, WeaponType.Mace };
        [SerializeField] float distance = 2.5f;
        [SerializeField] float spacing = 1.0f;
        [SerializeField] float dummyHeight = 1.6f;
        [Tooltip("Seconds the dummies may be out of view before they move back in front of the camera.")]
        [SerializeField] float recentreAfter = 1.5f;
        [Tooltip("Status messages (calibration, tracking lost, waiting for the opponent) sit this far above the " +
                 "view's centre, clear of the health bars, with the instruction below. While choosing, the text " +
                 "stays at the top, clear of the dummies.")]
        [SerializeField] float statusY = 0f;
        [SerializeField] float statusLineGap = 90f;

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
        PlayerId choosingFor;
        (Vector2 anchor, Vector2 pivot, Vector2 position) titleTop, instructionTop;
        bool textCentred;

        public bool IsSelecting => current != Stage.Closed;

        /// <summary>A weapon was locked (by the hub, or a demo pick) for the player choosing; GameAudio plays the equip sound.</summary>
        public event System.Action<WeaponType> WeaponLocked;

        /// <summary>
        /// Demo mode: what a pick button does (lock that weapon for that player). Null with the hub,
        /// where only the hub picks; set, the enemy's weapon is chosen here too, after yours.
        /// </summary>
        public System.Action<PlayerId, WeaponType> PickWeapon { get; set; }

        static bool Needs(PlayerState player) => player.weaponInitialized && !player.weaponSelected;

        /// <summary>Who chooses next: you first, then (demo mode only) the enemy.</summary>
        PlayerId? NextChooser =>
            Needs(gameState.Self) ? (PlayerId?)gameState.LocalPlayer
            : PickWeapon != null && Needs(gameState.Opponent) ? gameState.OpponentPlayer
            : null;

        bool ChoosingForSelf => choosingFor == gameState.LocalPlayer;

        /// <summary>Demo mode chooses before placing (the phone is in hand throughout); otherwise placement and calibration come first.</summary>
        bool PlacesFirst => PickWeapon == null;

        bool Calibrating => calibration != null && calibration.isActiveAndEnabled;

        bool ArenaReady =>
            placer == null || (placer.IsPlaced && (!Calibrating || calibration.IsCalibrated));

        void Start()
        {
            if (title != null)
                titleTop = (title.rectTransform.anchorMin, title.rectTransform.pivot, title.rectTransform.anchoredPosition);
            if (instruction != null)
                instructionTop = (instruction.rectTransform.anchorMin, instruction.rectTransform.pivot, instruction.rectTransform.anchoredPosition);
            SetText("", "");
            for (int i = 0; i < pickButtons.Length; i++)
            {
                int index = i;
                pickButtons[i].onClick.AddListener(() => Pick(index));
                pickButtons[i].gameObject.SetActive(false);
            }
        }

        void Update()
        {
            switch (current)
            {
                case Stage.Closed:
                    if (NextChooser is PlayerId next && (!PlacesFirst || ArenaReady))
                        Open(next);
                    else
                        ShowStatus();
                    break;
                case Stage.Choosing:
                    if (PlacesFirst && !ArenaReady)
                        Close();  // RESTART or a match reset picked the dummy up: place and calibrate first
                    else if (!Needs(gameState.Get(choosingFor)))
                        Confirm(gameState.Get(choosingFor).weapon);
                    else
                        KeepInView();
                    break;
                case Stage.Confirming:
                    stageTimer += Time.deltaTime;
                    Flash(stageTimer / confirmSeconds);
                    if (Needs(gameState.Get(choosingFor)))
                        Open(choosingFor);  // the hub dropped the lock (e.g. rebooted): choose again
                    else if (stageTimer >= confirmSeconds)
                    {
                        if (NextChooser is PlayerId following)
                            Open(following);  // demo mode: now the enemy's weapon
                        else
                            Close();
                    }
                    break;
            }
            FaceLabelsToCamera();
            PlacePickButtons();
        }

        void Open(PlayerId player)
        {
            Clear();
            current = Stage.Choosing;
            choosingFor = player;
            if (placer != null)
            {
                if (PlacesFirst)
                    placer.SetArenaVisible(false);
                else
                    placer.BeginPlacement();
                placer.enabled = false;
            }
            if (hud != null)
                hud.enabled = false;
            CentreText(false);

            stage = new GameObject("Weapon Select Stage").transform;
            for (int i = 0; i < weapons.Length; i++)
                SpawnDummy(weapons[i], i);
            PlaceInFrontOfCamera();

            if (PickWeapon != null)
                SetText(ChoosingForSelf ? "CHOOSE YOUR WEAPON" : "CHOOSE THE ENEMY'S WEAPON",
                    "Tap the button under a weapon");
            else
                SetText(weapons.Length == 1 ? "COPY THIS POSE" : "CHOOSE YOUR WEAPON",
                    $"Hold the pose for {holdSeconds:0} seconds");
        }

        void Pick(int index)
        {
            if (current == Stage.Choosing && PickWeapon != null && index < weapons.Length)
                PickWeapon(choosingFor, weapons[index]);
        }

        /// <summary>Demo mode: each pick button sits on the screen just under its dummy's feet.</summary>
        void PlacePickButtons()
        {
            bool show = current == Stage.Choosing && PickWeapon != null;
            for (int i = 0; i < pickButtons.Length; i++)
            {
                var button = pickButtons[i];
                bool visible = show && i < weapons.Length && dummies.ContainsKey(weapons[i]);
                if (visible)
                {
                    // Just above the feet, kept inside the view when the floor is out of frame.
                    Vector3 feet = dummies[weapons[i]].transform.parent.position + Vector3.up * 0.1f;
                    Vector3 screen = arCamera.WorldToScreenPoint(feet);
                    var parent = (RectTransform)button.transform.parent;
                    Vector2 local = default;
                    visible = screen.z > 0f && RectTransformUtility.ScreenPointToLocalPointInRectangle(parent, screen, null, out local);
                    if (visible)
                    {
                        float lowest = parent.rect.yMin + ((RectTransform)button.transform).rect.height + 40f;
                        button.transform.localPosition = new Vector3(local.x, Mathf.Max(local.y, lowest), 0f);
                    }
                }
                if (button.gameObject.activeSelf != visible)
                    button.gameObject.SetActive(visible);
            }
        }

        void Confirm(WeaponType weapon)
        {
            current = Stage.Confirming;
            stageTimer = 0f;
            chosen = weapon;
            WeaponLocked?.Invoke(weapon);
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
            {
                placer.SetArenaVisible(true);
                placer.enabled = true;
            }
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
            // Mirrored, so you copy it like a mirror: the dummy's weapon hand appears on the same side
            // as yours. The enemy's weapon (demo mode) is shown as the enemy will hold it.
            dummy.transform.localScale = new Vector3(ChoosingForSelf ? -scale : scale, scale, scale);
            MakeHologram(dummy, definition.accentColor);
            dummies[weapon] = dummy;

            labels.Add(Label(slot, definition.displayName.ToUpperInvariant(), definition.accentColor, dummyHeight + 0.25f, labelFont));
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

        static TextMesh Label(Transform parent, string text, Color colour, float height, Font font)
        {
            var label = new GameObject("Label").AddComponent<TextMesh>();
            label.transform.SetParent(parent, false);
            label.transform.localPosition = new Vector3(0f, height, 0f);
            label.text = text;
            label.font = font != null ? font : Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
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

        /// <summary>
        /// While closed: calibration's hint until confirmed, a warning while AR tracking is lost, else
        /// whether the opponent is still choosing.
        /// </summary>
        void ShowStatus()
        {
            CentreText(true);
            if (Calibrating && placer != null && placer.IsPlaced)
            {
                if (!calibration.IsCalibrated)
                {
                    SetText("FACE YOUR OPPONENT", calibration.Hint);
                    return;
                }
                if (calibration.TrackingLost)
                {
                    SetText("TRACKING LOST", "Look around the room slowly");
                    return;
                }
            }
            ShowOpponentStatus();
        }

        /// <summary>Once this player has a weapon, say so if the opponent is still choosing.</summary>
        void ShowOpponentStatus()
        {
            var opponent = gameState.Opponent;
            bool waiting = gameState.Self.weaponSelected && opponent.weaponInitialized && !opponent.weaponSelected;
            SetText("", waiting ? "Waiting for your opponent to choose a weapon" : "");
        }

        /// <summary>Status messages in the middle of the view; the selection's own text at the top.</summary>
        void CentreText(bool centre)
        {
            if (textCentred == centre)
                return;
            textCentred = centre;
            var middle = new Vector2(0.5f, 0.5f);
            Place(title, centre ? (middle, middle, new Vector2(0f, statusY)) : titleTop);
            Place(instruction, centre ? (middle, middle, new Vector2(0f, statusY - statusLineGap)) : instructionTop);
        }

        static void Place(Text text, (Vector2 anchor, Vector2 pivot, Vector2 position) at)
        {
            if (text == null)
                return;
            var rect = text.rectTransform;
            rect.anchorMin = rect.anchorMax = at.anchor;
            rect.pivot = at.pivot;
            rect.anchoredPosition = at.position;
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
