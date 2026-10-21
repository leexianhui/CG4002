using System.Collections.Generic;
using CG4002.Combat;
using CG4002.FX;
using CG4002.Models;
using UnityEngine;
using UnityEngine.InputSystem;

namespace CG4002.Showcase
{
    /// <summary>
    /// Showcase scene only (no AR, no state engine): keyboard controls that drive AvatarView
    /// directly, for previewing models, animations and effects.
    /// </summary>
    public class ShowcaseController : MonoBehaviour
    {
        [SerializeField] AvatarView avatar;
        [SerializeField] WeaponCatalog catalog;
        [SerializeField] CombatFxController fx;

        [Header("Auto demo")]
        [SerializeField] bool autoDemoOnStart;
        [SerializeField] float autoDemoStepSeconds = 1.4f;

        const int LogLines = 9;

        readonly Queue<string> log = new();
        int weaponIndex;
        int moveIndex;
        bool slowMotion;
        bool autoDemo;
        float autoTimer;
        int autoStep;
        GUIStyle panelStyle;

        WeaponDefinition Current => catalog.weapons[weaponIndex];

        void Start()
        {
            avatar.ChargeStarted += (_, slot) => Log($"event  ChargeStart ({slot})");
            avatar.StrikeOpened += (_, slot) => Log($"event  StrikeOpen  ({slot})");
            avatar.StrikeClosed += (_, slot) => Log($"event  StrikeClose ({slot})");

            Equip(0);
            autoDemo = autoDemoOnStart;
        }

        void Update()
        {
            if (Keyboard.current != null)
                HandleKeys(Keyboard.current);
            if (autoDemo)
                TickAutoDemo();
        }

        void HandleKeys(Keyboard keyboard)
        {
            if (keyboard.digit1Key.wasPressedThisFrame) Equip(0);
            if (keyboard.digit2Key.wasPressedThisFrame) Equip(1);
            if (keyboard.digit3Key.wasPressedThisFrame) Equip(2);

            if (keyboard.lKey.wasPressedThisFrame) PerformNextMove();
            if (keyboard.cKey.wasPressedThisFrame) PerformFirstMoveIn(AttackSlot.Charged);
            if (keyboard.gKey.wasPressedThisFrame) ToggleGuard();
            if (keyboard.hKey.wasPressedThisFrame) Hit();

            if (keyboard.leftArrowKey.wasPressedThisFrame) SelectMove(-1);
            if (keyboard.rightArrowKey.wasPressedThisFrame) SelectMove(+1);
            if (keyboard.spaceKey.wasPressedThisFrame) Perform(Current.moves[moveIndex].action);

            if (keyboard.tKey.wasPressedThisFrame) ToggleSlowMotion();
            if (keyboard.pKey.wasPressedThisFrame)
            {
                autoDemo = !autoDemo;
                autoTimer = 0f;
                Log(autoDemo ? "auto demo ON" : "auto demo OFF");
            }
        }

        // ---- Keyboard actions ----------------------------------------------------------------

        void Equip(int index)
        {
            weaponIndex = Mathf.Clamp(index, 0, catalog.weapons.Length - 1);
            moveIndex = 0;
            avatar.Equip(Current);
            Log($"WEAPON_CHANGED  {Current.type}");
        }

        void Perform(ActionType action)
        {
            if (!Current.TryGetSlot(action, out var slot))
            {
                Log($"ACTION {action}: not in {Current.type} move set");
                return;
            }
            avatar.PlayAttack(slot);
            Log($"ACTION {action} -> {slot}");
        }

        /// <summary>Steps through the weapon's moves, so repeated presses show every attack.</summary>
        void PerformNextMove()
        {
            SelectMove(+1);
            Perform(Current.moves[moveIndex].action);
        }

        void PerformFirstMoveIn(AttackSlot slot)
        {
            foreach (var move in Current.moves)
            {
                if (move.slot == slot)
                {
                    Perform(move.action);
                    return;
                }
            }
        }

        void ToggleGuard()
        {
            avatar.SetGuard(!avatar.IsGuarding);
            Log($"GUARD_STATE  {(avatar.IsGuarding ? "Guarding" : "Idle")}");
        }

        void Hit()
        {
            fx.PlayHit(avatar);
            Log("COMBAT_OUTCOME  HIT");
        }

        void SelectMove(int delta)
        {
            int count = Current.moves.Length;
            moveIndex = (moveIndex + delta + count) % count;
        }

        void ToggleSlowMotion()
        {
            slowMotion = !slowMotion;
            Time.timeScale = slowMotion ? 0.25f : 1f;
        }

        void TickAutoDemo()
        {
            autoTimer += Time.deltaTime;
            if (autoTimer < autoDemoStepSeconds)
                return;
            autoTimer = 0f;

            switch (autoStep % 6)
            {
                case 0: PerformNextMove(); break;
                case 1: PerformFirstMoveIn(AttackSlot.Charged); break;
                case 2: ToggleGuard(); break;
                case 3: Hit(); break;
                case 4: ToggleGuard(); break;
                case 5: Equip((weaponIndex + 1) % catalog.weapons.Length); break;
            }
            autoStep++;
        }

        void Log(string line)
        {
            log.Enqueue($"{Time.time,7:0.00}  {line}");
            while (log.Count > LogLines)
                log.Dequeue();
        }

        void OnDisable() => Time.timeScale = 1f;

        // ---- Overlay -------------------------------------------------------------------------

        void OnGUI()
        {
            panelStyle ??= new GUIStyle(GUI.skin.box)
            {
                alignment = TextAnchor.UpperLeft,
                fontSize = 15,
                richText = true,
                padding = new RectOffset(12, 12, 10, 10),
            };

            var move = Current.moves[moveIndex];
            string text =
                $"<b>{Current.displayName}</b>   guard: {(avatar.IsGuarding ? "<color=#7CFC00>ON</color>" : "off")}" +
                $"   speed: {(slowMotion ? "0.25x" : "1x")}   auto: {(autoDemo ? "on" : "off")}\n" +
                $"selected move: <b>{move.action}</b> -> {move.slot}\n\n" +
                "1/2/3 weapon   L next move   C charged   G guard   H hit\n" +
                "<-/-> pick move   Space perform   T slow-mo   P auto demo\n" +
                "right-drag orbit   scroll zoom\n\n" +
                string.Join("\n", log);

            GUI.Box(new Rect(12, 12, 560, 330), text, panelStyle);
        }
    }
}
