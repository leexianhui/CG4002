using CG4002.State;
using UnityEngine;

namespace CG4002.AR
{
    /// <summary>
    /// Sends this player's position to the engine: the phone camera's pose, converted to arena
    /// coordinates, a few times a second once the arena is placed.
    ///
    /// The arena origin is wherever this phone placed it. Two phones only agree on positions
    /// if both place the arena on the same physical spot (see Docs/StateEngine.md, "Open decisions").
    /// </summary>
    public class PositionReporter : MonoBehaviour
    {
        [SerializeField] Camera arCamera;
        [SerializeField] Transform arena;
        [SerializeField] DummyPlacer placer;
        [SerializeField] MessageDispatcher dispatcher;
        [SerializeField] GameState gameState;
        [SerializeField] float sendsPerSecond = 10f;

        float nextSendTime;

        void Update()
        {
            if (!placer.IsPlaced || Time.time < nextSendTime)
                return;
            nextSendTime = Time.time + 1f / sendsPerSecond;

            var (position, heading) = ToArena(arena, arCamera.transform.position, arCamera.transform.forward);
            dispatcher.Outbound.Enqueue(new PositionMessage
            {
                player = gameState.LocalPlayer,
                position = position,
                headingDegrees = heading,
                monotonicTimestampMs = (long)(System.Diagnostics.Stopwatch.GetTimestamp() *
                    (1000.0 / System.Diagnostics.Stopwatch.Frequency)),
            });
        }

        /// <summary>
        /// A world-space position and facing direction, as arena position (metres) and heading
        /// (degrees clockwise from the arena's +Z, looking down, ignoring tilt).
        /// </summary>
        public static (Vector3 position, float headingDegrees) ToArena(Transform arena, Vector3 worldPosition, Vector3 worldForward)
        {
            Vector3 forward = arena.InverseTransformDirection(worldForward);
            float heading = Mathf.Atan2(forward.x, forward.z) * Mathf.Rad2Deg;
            return (arena.InverseTransformPoint(worldPosition), heading);
        }
    }
}
