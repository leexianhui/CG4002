using System;
using UnityEngine;

namespace CG4002.State
{
    /// <summary>
    /// The visualizer's boundary with the Game Engine: two queues.
    ///   Inbound   engine to visualizer. Drained here once per frame: each message is applied
    ///             to GameState (things that are), then raised as an event (things that happen).
    ///   Outbound  visualizer to engine. Filled by PositionReporter, DummyPlacer and DebugPanel;
    ///             drained by the engine side (the comms client, or LocalGameRules offline).
    ///             Never applied to GameState: state only changes when the engine says so.
    ///
    /// Execution order -100 runs this before every other Update, so everything that renders
    /// this frame sees this frame's state.
    /// </summary>
    [DefaultExecutionOrder(-100)]
    public class MessageDispatcher : MonoBehaviour
    {
        [SerializeField] GameState gameState;

        public MessageQueue Inbound { get; } = new();
        public MessageQueue Outbound { get; } = new();

        /// <summary>A player performed an attack. Raised after GameState is updated.</summary>
        public event Action<ActionMessage> ActionReceived;

        /// <summary>An attack was resolved. Raised after GameState has the new health.</summary>
        public event Action<CombatOutcomeMessage> OutcomeReceived;

        void Update()
        {
            // Drain everything, not one per frame, so a burst doesn't turn into visible lag.
            while (Inbound.TryDequeue(out var message))
                Dispatch(message);
        }

        void Dispatch(Message message)
        {
            if (message.schemaVersion != Message.CurrentSchemaVersion)
            {
                Debug.LogWarning($"Ignoring {message.GetType().Name} with schema version {message.schemaVersion}.");
                return;
            }

            // State first, so anything reacting to the event below already sees the new values.
            gameState.Apply(message);

            switch (message)
            {
                case ActionMessage action:
                    ActionReceived?.Invoke(action);
                    break;
                case CombatOutcomeMessage outcome:
                    OutcomeReceived?.Invoke(outcome);
                    break;
            }
        }
    }
}
