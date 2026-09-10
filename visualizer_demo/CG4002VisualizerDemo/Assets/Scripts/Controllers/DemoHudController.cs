// Wires EngineClient's events to the minimal demo scene: two avatars, two HP bars, a weapon label
// per player, and a scrolling combat-event feed. Deliberately not ARFoundation/camera passthrough
// (visualizer_demo/README.md) - just a legible top-down HUD proving the same messages
// comms/src/cg_comms/mock/visualizer_sim.py already logs are flowing end-to-end.

using System.Collections.Generic;
using Cg4002.VisualizerDemo.Net;
using UnityEngine;
using UnityEngine.UI;

namespace Cg4002.VisualizerDemo.Controllers
{
    public class DemoHudController : MonoBehaviour
    {
        public EngineClient Client;

        [Header("Player 1")]
        public Transform Player1Avatar;
        public Slider Player1HpBar;
        public Text Player1WeaponLabel;

        [Header("Player 2")]
        public Transform Player2Avatar;
        public Slider Player2HpBar;
        public Text Player2WeaponLabel;

        [Header("Feed / status")]
        public Text CombatFeedText;
        public Text ConnectionStatusText;
        public int MaxFeedLines = 8;

        [Header("Position echo (demo only - not read from real motion)")]
        public bool SendSyntheticPosition = true;
        public float PositionSendIntervalSeconds = 0.5f;

        private readonly Queue<string> _feedLines = new Queue<string>();
        private float _positionTimer;

        private void Start()
        {
            Client.OnCombatEvent += HandleCombatEvent;
            Client.OnMatchState += HandleMatchState;
            Client.OnWeaponChanged += HandleWeaponChanged;
            Client.OnConnectionStatus += HandleConnectionStatus;
            Client.Connect();
        }

        private void OnDestroy()
        {
            if (Client == null) return;
            Client.OnCombatEvent -= HandleCombatEvent;
            Client.OnMatchState -= HandleMatchState;
            Client.OnWeaponChanged -= HandleWeaponChanged;
            Client.OnConnectionStatus -= HandleConnectionStatus;
        }

        private void Update()
        {
            if (!SendSyntheticPosition || !Client.IsConnected) return;
            _positionTimer += Time.deltaTime;
            if (_positionTimer < PositionSendIntervalSeconds) return;
            _positionTimer = 0f;
            // Demo stand-in for real device tracking - just echoes wherever this player's avatar
            // already is in scene space, matching visualizer_sim.py's periodic POSITION send.
            var t = Client.PlayerId == 1 ? Player1Avatar : Player2Avatar;
            if (t == null) return;
            Client.SendPosition(t.position.x, t.position.z, t.eulerAngles.y);
        }

        private void HandleConnectionStatus(string status)
        {
            if (ConnectionStatusText != null) ConnectionStatusText.text = $"engine: {status}";
        }

        private void HandleWeaponChanged(WeaponChanged msg)
        {
            var label = msg.PlayerId == 1 ? Player1WeaponLabel : Player2WeaponLabel;
            if (label != null) label.text = msg.Weapon;
        }

        private void HandleMatchState(MatchState msg)
        {
            if (msg.Hp == null) return;
            if (msg.Hp.TryGetValue("1", out int hp1) && Player1HpBar != null) Player1HpBar.value = hp1;
            if (msg.Hp.TryGetValue("2", out int hp2) && Player2HpBar != null) Player2HpBar.value = hp2;
            AppendFeedLine($"[MATCH] phase={msg.Phase} winner={msg.WinnerId} reason={msg.Reason}");
        }

        private void HandleCombatEvent(CombatEvent msg)
        {
            AppendFeedLine($"P{msg.AttackerId} {msg.Action} -> P{msg.DefenderId}: {msg.Outcome} " +
                            $"(dmg {msg.Damage}, hp {msg.AttackerHp}/{msg.DefenderHp})");
            if (msg.Outcome == "HIT" || msg.Outcome == "BLOCK" || msg.Outcome == "PARRY")
            {
                var target = msg.DefenderId == 1 ? Player1Avatar : Player2Avatar;
                if (target != null) StartCoroutine(FlashAvatar(target));
            }
        }

        private void AppendFeedLine(string line)
        {
            _feedLines.Enqueue(line);
            while (_feedLines.Count > MaxFeedLines) _feedLines.Dequeue();
            if (CombatFeedText != null) CombatFeedText.text = string.Join("\n", _feedLines);
        }

        private System.Collections.IEnumerator FlashAvatar(Transform avatar)
        {
            var renderer = avatar.GetComponent<Renderer>();
            if (renderer == null) yield break;
            var original = renderer.material.color;
            renderer.material.color = Color.red;
            yield return new WaitForSeconds(0.15f);
            renderer.material.color = original;
        }
    }
}
