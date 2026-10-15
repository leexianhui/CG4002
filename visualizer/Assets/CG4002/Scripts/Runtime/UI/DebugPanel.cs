using CG4002.AR;
using CG4002.State;
using UnityEngine;
using UnityEngine.UI;

namespace CG4002.UI
{
    /// <summary>
    /// On-screen buttons for testing on the phone:
    ///   RESET        ask the engine for a new match
    ///   MOVE DUMMY   pick the dummy up; the next tap on a detected surface places it again
    ///   TRACKING     show or hide the marker outlines and readout (tracking carries on either way)
    /// </summary>
    public class DebugPanel : MonoBehaviour
    {
        [SerializeField] MessageDispatcher dispatcher;
        [SerializeField] GameState gameState;
        [SerializeField] DummyPlacer placer;
        [SerializeField] TrackedImageDebug trackingOverlay;

        [SerializeField] Button resetButton;
        [SerializeField] Button moveDummyButton;
        [SerializeField] Button trackingButton;
        [SerializeField] Text trackingLabel;

        void Awake()
        {
            resetButton.onClick.AddListener(RequestNewMatch);
            moveDummyButton.onClick.AddListener(placer.BeginPlacement);
            trackingButton.onClick.AddListener(ToggleTrackingOverlay);
            UpdateTrackingLabel();
        }

        void RequestNewMatch() =>
            dispatcher.Outbound.Enqueue(new StartMatchRequestMessage { player = gameState.LocalPlayer });

        void ToggleTrackingOverlay()
        {
            trackingOverlay.Visible = !trackingOverlay.Visible;
            UpdateTrackingLabel();
        }

        void UpdateTrackingLabel() =>
            trackingLabel.text = trackingOverlay.Visible ? "TRACKING: ON" : "TRACKING: OFF";
    }
}
