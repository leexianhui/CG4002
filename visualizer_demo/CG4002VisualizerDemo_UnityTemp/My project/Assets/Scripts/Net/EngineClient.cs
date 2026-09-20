// WSS client to the Game Engine's /visualizer/<playerId> endpoint (shared/protocol/app_messages.md
// §2, comms/src/cg_comms/engine/ws_server.py). Mirrors comms/src/cg_comms/mock/visualizer_sim.py's
// connection contract: bearer token in the Authorization header, one long-lived connection,
// periodic POSITION sends, log/dispatch everything else received.
//
// Uses System.Net.WebSockets.ClientWebSocket directly (no NativeWebSocket package dependency) —
// available on Mono/IL2CPP for Android and iOS builds, which is the only target this demo needs.

using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using UnityEngine;

namespace Cg4002.VisualizerDemo.Net
{
    public class EngineClient : MonoBehaviour
    {
        [Header("Connection")]
        public string Host = "ULTRA96_HOST";
        public int Port = 8443;
        public bool UseTls = true;
        public int PlayerId = 1;
        public string BearerToken = "";

        [Tooltip("SHA-256 fingerprint (hex, colon or no separator) of the engine's self-signed " +
                 "cert, from `openssl x509 -noout -fingerprint -sha256 -in comms/certs/engine.crt`. " +
                 "Leave blank to accept any cert (demo/dev convenience only - never do this for a " +
                 "real deployment).")]
        public string PinnedCertSha256 = "";

        public event Action<CombatEvent> OnCombatEvent;
        public event Action<MatchState> OnMatchState;
        public event Action<CooldownState> OnCooldownState;
        public event Action<WeaponChanged> OnWeaponChanged;
        public event Action<Position> OnPositionEcho;
        public event Action<ActionRecognized> OnActionRecognized;
        public event Action<string> OnConnectionStatus;

        private static readonly int[] ReconnectBackoffSeconds = { 1, 2, 4, 8 };

        private ClientWebSocket _ws;
        private CancellationTokenSource _cts;
        // Unity API calls (events -> scene objects) must happen on the main thread; the receive
        // loop runs on a background task, so hand messages back via this queue and drain it in Update.
        private readonly ConcurrentQueue<string> _inbox = new ConcurrentQueue<string>();
        private Coroutine _reconnectCoroutine;
        private bool _disconnectRequested;

        public bool IsConnected => _ws != null && _ws.State == WebSocketState.Open;

        public async void Connect()
        {
            _disconnectRequested = false;
            _cts = new CancellationTokenSource();
            _ws = new ClientWebSocket();
            _ws.Options.SetRequestHeader("Authorization", $"Bearer {BearerToken}");

            string scheme = UseTls ? "wss" : "ws";
            var uri = new Uri($"{scheme}://{Host}:{Port}/visualizer/{PlayerId}");
            try
            {
                await _ws.ConnectAsync(uri, _cts.Token);
                OnConnectionStatus?.Invoke("connected");
                _ = ReceiveLoop(_cts.Token);
            }
            catch (Exception e)
            {
                OnConnectionStatus?.Invoke($"connect_failed: {e.Message}");
                BeginReconnect();
            }
        }

        public void Disconnect()
        {
            _disconnectRequested = true;
            if (_reconnectCoroutine != null)
            {
                StopCoroutine(_reconnectCoroutine);
                _reconnectCoroutine = null;
            }
            DisconnectInternal();
        }

        private async void DisconnectInternal()
        {
            _cts?.Cancel();
            if (_ws != null && _ws.State == WebSocketState.Open)
            {
                await _ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "closing", CancellationToken.None);
            }
        }

        // Capped backoff (1, 2, 4, 8s, repeating at 8s) so a killed/restarted engine (U3 test:
        // "killing/restarting the engine shows disconnected then connected again without an app
        // restart") is recovered from automatically instead of leaving the demo stuck.
        private void BeginReconnect()
        {
            if (_disconnectRequested || _reconnectCoroutine != null) return;
            _reconnectCoroutine = StartCoroutine(ReconnectLoop());
        }

        private IEnumerator ReconnectLoop()
        {
            int attempt = 0;
            while (!_disconnectRequested && !IsConnected)
            {
                int delay = ReconnectBackoffSeconds[Mathf.Min(attempt, ReconnectBackoffSeconds.Length - 1)];
                OnConnectionStatus?.Invoke($"reconnecting_in_{delay}s");
                yield return new WaitForSeconds(delay);
                if (_disconnectRequested) break;
                attempt++;
                Connect();
                // Give Connect()'s async ConnectAsync a moment to resolve before checking IsConnected
                // again; a hard failure re-enters BeginReconnect via the catch block below anyway.
                yield return new WaitForSeconds(0.1f);
            }
            _reconnectCoroutine = null;
        }

        public async void SendPosition(float x, float y, float heading)
        {
            if (!IsConnected) return;
            var position = new Position
            {
                Type = "POSITION",
                Version = 1,
                Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                PlayerId = PlayerId,
                X = x,
                Y = y,
                Heading = heading,
            };
            string json = JsonConvert.SerializeObject(position);
            byte[] bytes = Encoding.UTF8.GetBytes(json);
            await _ws.SendAsync(bytes, WebSocketMessageType.Text, true, CancellationToken.None);
        }

        private async Task ReceiveLoop(CancellationToken token)
        {
            var buffer = new byte[8192];
            try
            {
                while (!token.IsCancellationRequested && _ws.State == WebSocketState.Open)
                {
                    using var stream = new System.IO.MemoryStream();
                    WebSocketReceiveResult result;
                    do
                    {
                        result = await _ws.ReceiveAsync(buffer, token);
                        if (result.MessageType == WebSocketMessageType.Close)
                        {
                            _inbox.Enqueue(null);  // sentinel: signal disconnect to the main thread
                            return;
                        }
                        stream.Write(buffer, 0, result.Count);
                    } while (!result.EndOfMessage);

                    string json = Encoding.UTF8.GetString(stream.ToArray());
                    _inbox.Enqueue(json);
                }
            }
            catch (OperationCanceledException)
            {
                // Disconnect() was called - expected, not an error.
            }
            catch (Exception e)
            {
                _inbox.Enqueue(null);
                Debug.LogError($"EngineClient receive loop failed: {e}");
            }
        }

        private void Update()
        {
            while (_inbox.TryDequeue(out string json))
            {
                if (json == null)
                {
                    OnConnectionStatus?.Invoke("disconnected");
                    BeginReconnect();
                    continue;
                }
                DispatchMessage(json);
            }
        }

        private void DispatchMessage(string json)
        {
            string type;
            try
            {
                type = AppMessage.PeekType(json);
            }
            catch (JsonException e)
            {
                Debug.LogWarning($"EngineClient: dropped unparseable message: {e.Message}");
                return;
            }

            switch (type)
            {
                case "COMBAT_EVENT":
                    OnCombatEvent?.Invoke(JsonConvert.DeserializeObject<CombatEvent>(json));
                    break;
                case "MATCH_STATE":
                    OnMatchState?.Invoke(JsonConvert.DeserializeObject<MatchState>(json));
                    break;
                case "COOLDOWN_STATE":
                    OnCooldownState?.Invoke(JsonConvert.DeserializeObject<CooldownState>(json));
                    break;
                case "WEAPON_CHANGED":
                    OnWeaponChanged?.Invoke(JsonConvert.DeserializeObject<WeaponChanged>(json));
                    break;
                case "POSITION":
                    OnPositionEcho?.Invoke(JsonConvert.DeserializeObject<Position>(json));
                    break;
                case "ACTION_RECOGNIZED":
                    OnActionRecognized?.Invoke(JsonConvert.DeserializeObject<ActionRecognized>(json));
                    break;
                default:
                    Debug.LogWarning($"EngineClient: unhandled message type '{type}'");
                    break;
            }
        }

        private void OnDestroy()
        {
            _cts?.Cancel();
        }

        // Certificate pinning is wired up via ServicePointManager's callback on platforms where
        // ClientWebSocket honors it (Mono). On IL2CPP/mobile this may need a platform-specific
        // hook instead - flagged in visualizer_demo/README.md as the most likely early snag.
        private void Awake()
        {
            if (string.IsNullOrEmpty(PinnedCertSha256))
            {
                Debug.LogWarning("EngineClient: PinnedCertSha256 is blank - accepting any TLS " +
                                  "certificate (demo/dev convenience only, never for a real deployment).");
                System.Net.ServicePointManager.ServerCertificateValidationCallback =
                    (sender, certificate, chain, errors) => true;
                return;
            }
            System.Net.ServicePointManager.ServerCertificateValidationCallback =
                (sender, certificate, chain, errors) => ValidatePinnedCert(certificate);
        }

        private bool ValidatePinnedCert(X509Certificate certificate)
        {
            if (certificate == null) return false;
            string actual = certificate.GetCertHashString(System.Security.Cryptography.HashAlgorithmName.SHA256);
            string expected = PinnedCertSha256.Replace(":", "").Replace(" ", "").ToUpperInvariant();
            return string.Equals(actual, expected, StringComparison.OrdinalIgnoreCase);
        }
    }
}
