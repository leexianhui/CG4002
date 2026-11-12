using CG4002.AR;
using CG4002.Net;
using UnityEngine;
using UnityEngine.UI;

namespace CG4002.UI
{
    /// <summary>
    /// RE-PLACE DUMMY, bottom centre: shown after the dummy is placed and until calibration confirms
    /// (OnlineScene), so a placement you don't like can be redone before the headset goes on. Tapping
    /// it picks the dummy up: the view returns to full screen with the floor planes, and the next tap
    /// on one places it again.
    ///
    /// Its canvas draws over split screen's output and isn't rendered into the eyes, so the tap lands
    /// where the button is drawn.
    /// </summary>
    public class ReplaceDummyButton : MonoBehaviour
    {
        [SerializeField] DummyPlacer placer;
        [SerializeField] SharedArenaCalibration calibration;
        [SerializeField] Button button;

        void Awake() => button.onClick.AddListener(placer.BeginPlacement);

        void Update()
        {
            bool show = placer.IsPlaced && calibration != null && calibration.isActiveAndEnabled && !calibration.IsCalibrated;
            if (button.gameObject.activeSelf != show)
                button.gameObject.SetActive(show);
        }
    }
}
