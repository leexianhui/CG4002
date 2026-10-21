using System.Collections;
using CG4002.Combat;
using CG4002.State;
using UnityEngine;
using UnityEngine.InputSystem;

namespace CG4002.Offline
{
    /// <summary>
    /// Offline only: plays the opponent (guards, side-steps, optional attacks) and adds
    /// editor keys for testing without the phone or the prop. Calls LocalGameRules directly,
    /// because the opponent is on the engine's side of the boundary.
    /// </summary>
    public class MockDataInjector : MonoBehaviour
    {
        [SerializeField] LocalGameRules rules;
        [SerializeField] PlayerId dummy = PlayerId.P2;
        [SerializeField] PlayerId localPlayer = PlayerId.P1;

        [Header("Dummy behaviour")]
        [SerializeField] bool dummyGuards = true;
        [Tooltip("Seconds the dummy holds its guard each time.")]
        [SerializeField] float guardDuration = 1.5f;
        [Tooltip("Seconds between guards (random within this range).")]
        [SerializeField] Vector2 guardInterval = new(2.5f, 4.5f);

        [SerializeField] bool dummyMoves = true;
        [Tooltip("How far the dummy steps side to side from its placed spot, metres.")]
        [SerializeField] float stepDistance = 0.25f;
        [SerializeField] float stepInterval = 3f;

        [SerializeField] bool dummyAttacks;
        [SerializeField] float attackInterval = 5f;

        // The opponent has always chosen already, so only the local player sees weapon selection.
        void Start() => rules.LockWeapon(dummy, WeaponType.SwordAndShield);

        void OnEnable()
        {
            if (dummyGuards) StartCoroutine(GuardRoutine());
            if (dummyMoves) StartCoroutine(MoveRoutine());
            if (dummyAttacks) StartCoroutine(AttackRoutine());
        }

        void OnDisable() => StopAllCoroutines();

        IEnumerator GuardRoutine()
        {
            while (true)
            {
                yield return new WaitForSeconds(Random.Range(guardInterval.x, guardInterval.y));
                rules.SetGuard(dummy, true);
                yield return new WaitForSeconds(guardDuration);
                rules.SetGuard(dummy, false);
            }
        }

        IEnumerator MoveRoutine()
        {
            float side = 1f;
            while (true)
            {
                yield return new WaitForSeconds(stepInterval);
                rules.Move(dummy, new Vector3(side * stepDistance, 0f, 0f), 0f);
                side = -side;
            }
        }

        IEnumerator AttackRoutine()
        {
            while (true)
            {
                yield return new WaitForSeconds(attackInterval);
                rules.Attack(dummy, ActionType.HorizontalSwing);
            }
        }

        // Editor testing: H = my sword hits the dummy, G = toggle the dummy's guard,
        // C = the dummy attacks me, R = restart the match,
        // 1 = I hold the sword stance until the hub locks it (with Require Weapon Selection on).
        void Update()
        {
            var keyboard = Keyboard.current;
            if (keyboard == null)
                return;

            if (keyboard.hKey.wasPressedThisFrame) rules.Attack(localPlayer, ActionType.HorizontalSwing);
            if (keyboard.gKey.wasPressedThisFrame) rules.SetGuard(dummy, !rules.IsGuarding(dummy));
            if (keyboard.cKey.wasPressedThisFrame) rules.Attack(dummy, ActionType.HorizontalSwing);
            if (keyboard.rKey.wasPressedThisFrame) rules.StartMatch();
            if (keyboard.digit1Key.wasPressedThisFrame) rules.LockWeapon(localPlayer, WeaponType.SwordAndShield);
        }
    }
}
