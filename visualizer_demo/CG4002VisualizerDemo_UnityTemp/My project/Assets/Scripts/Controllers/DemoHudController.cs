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
        public Text EngineStatusLabel;
        public Text Player1PresenceLabel;
        public Text Player2PresenceLabel;
        public int MaxFeedLines = 14;

        [Header("Position echo (demo only - not read from real motion)")]
        public bool SendSyntheticPosition = true;
        public float PositionSendIntervalSeconds = 0.5f;

        private readonly Queue<string> _feedLines = new Queue<string>();
        private float _positionTimer;
        // Tracks each player's last-announced weapon so the feed only prints a "weapon = X" line on
        // change (§9.4) - the GPIO 4 press itself prints nothing; WEAPON_CHANGED is not broadcast to
        // visualizers, so this is inferred from ActionRecognized.Weapon instead.
        private readonly Dictionary<int, string> _lastWeapon = new Dictionary<int, string>();
        // §9.4's rendering rules fire on CHANGE, not on every MATCH_STATE snapshot (which is
        // rebroadcast on every connect/disconnect, not just on real HP/presence changes). The first
        // snapshot ever seen establishes the baseline silently - matching the plan's own P1-only
        // transcript, which shows no "> P1 CONNECTED" line for a connection that was already present
        // when the HUD started.
        private readonly Dictionary<int, int> _lastHp = new Dictionary<int, int>();
        private readonly Dictionary<int, bool> _lastConnected = new Dictionary<int, bool>();

        private void Start()
        {
            Client.OnCombatEvent += HandleCombatEvent;
            Client.OnMatchState += HandleMatchState;
            Client.OnWeaponChanged += HandleWeaponChanged;
            Client.OnActionRecognized += HandleActionRecognized;
            Client.OnConnectionStatus += HandleConnectionStatus;
            Client.Connect();
        }

        private void OnDestroy()
        {
            if (Client == null) return;
            Client.OnCombatEvent -= HandleCombatEvent;
            Client.OnMatchState -= HandleMatchState;
            Client.OnWeaponChanged -= HandleWeaponChanged;
            Client.OnActionRecognized -= HandleActionRecognized;
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
            Client.SendUnityPosition(t.position.x, t.position.z, t.eulerAngles.y);
        }

        private void HandleConnectionStatus(string status)
        {
            if (ConnectionStatusText != null) ConnectionStatusText.text = $"engine: {status}";
            if (EngineStatusLabel != null) EngineStatusLabel.text = $"Engine: {status.ToUpperInvariant()}";
        }

        private void HandleWeaponChanged(WeaponChanged msg)
        {
            var label = msg.PlayerId == 1 ? Player1WeaponLabel : Player2WeaponLabel;
            if (label != null) label.text = msg.Weapon;
        }

        private void HandleMatchState(MatchState msg)
        {
            if (msg.Reason == "reset") {
                _lastWeapon.Clear();
                if (Player1WeaponLabel != null) Player1WeaponLabel.text = "-";
                if (Player2WeaponLabel != null) Player2WeaponLabel.text = "-";
                AppendFeedLine("> MATCH RESET");
            }
            if (msg.Hp != null)
            {
                if (msg.Hp.TryGetValue("1", out int hp1)) UpdateHp(1, hp1, Player1HpBar);
                if (msg.Hp.TryGetValue("2", out int hp2)) UpdateHp(2, hp2, Player2HpBar);
            }
            UpdatePresence(1, msg.Connected, Player1PresenceLabel);
            UpdatePresence(2, msg.Connected, Player2PresenceLabel);
        }

        private void UpdateHp(int playerId, int hp, Slider bar)
        {
            if (bar != null) bar.value = hp;
            if (_lastHp.TryGetValue(playerId, out int previous) && previous != hp)
            {
                AppendFeedLine($"> P{playerId} HP = {hp}");
            }
            _lastHp[playerId] = hp;
        }

        private void UpdatePresence(int playerId, Dictionary<string, bool> connected, Text label)
        {
            bool isConnected = connected != null
                && connected.TryGetValue(playerId.ToString(), out bool value)
                && value;
            if (label != null) label.text = isConnected ? $"P{playerId}: CONNECTED" : $"P{playerId}: OFFLINE";
            if (_lastConnected.TryGetValue(playerId, out bool previous) && previous != isConnected)
            {
                AppendFeedLine(isConnected ? $"> P{playerId} CONNECTED" : $"> P{playerId} OFFLINE");
            }
            _lastConnected[playerId] = isConnected;
        }

        private void HandleActionRecognized(ActionRecognized msg)
        {
            if (!_lastWeapon.TryGetValue(msg.PlayerId, out string previousWeapon) || previousWeapon != msg.Weapon)
            {
                _lastWeapon[msg.PlayerId] = msg.Weapon;
                AppendFeedLine($"> P{msg.PlayerId} weapon = {msg.Weapon}");
            }

            string trace = string.IsNullOrEmpty(msg.DemoTraceId) ? "" : $" [{msg.DemoTraceId}]";
            bool isGuard = msg.Action == "IDLE" && msg.GuardState == "GUARDING";
            if (isGuard)
            {
                AppendFeedLine($"> P{msg.PlayerId} used GUARD{trace}");
                return;
            }
            if (msg.Action == "IDLE") return;

            AppendFeedLine($"> P{msg.PlayerId} used {msg.Action}{trace}");
        }

        private void HandleCombatEvent(CombatEvent msg)
        {
            AppendFeedLine($"> P{msg.AttackerId} {msg.Outcome} P{msg.DefenderId}");
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
