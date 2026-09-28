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
        private CancellationTokenSource _lifetime;
        private readonly SemaphoreSlim _sendLock = new SemaphoreSlim(1, 1);
        private readonly ConcurrentQueue<string> _inbox = new ConcurrentQueue<string>();
        private bool _resumeOnForeground;

        public bool IsConnected => _ws != null && _ws.State == WebSocketState.Open;

        public void Connect()
        {
            if (_lifetime != null) return; // exactly one reconnect owner
            _lifetime = new CancellationTokenSource();
            _ = ConnectionLoop(_lifetime);
        }

        public void Disconnect()
        {
            var lifetime = _lifetime;
            _lifetime = null;
            lifetime?.Cancel();
            // Abort synchronously; an old asynchronous close must never dispose a replacement socket.
            _ws?.Abort();
            _ws = null;
            while (_inbox.TryDequeue(out _)) { }
        }

        private async Task ConnectionLoop(CancellationTokenSource lifetime)
        {
            int attempt = 0;
            try
            {
                while (!lifetime.IsCancellationRequested)
                {
                    using var socket = new ClientWebSocket();
                    socket.Options.SetRequestHeader("Authorization", $"Bearer {BearerToken}");
                    socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(5);
                    var started = System.Diagnostics.Stopwatch.StartNew();
                    try
                    {
                        using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token))
                        {
                            timeout.CancelAfter(TimeSpan.FromSeconds(5));
                            string scheme = UseTls ? "wss" : "ws";
                            await socket.ConnectAsync(new Uri($"{scheme}://{Host}:{Port}/visualizer/{PlayerId}"),
                                                      timeout.Token);
                        }
                        if (lifetime.IsCancellationRequested) break;
                        _ws = socket;
                        OnConnectionStatus?.Invoke("connected");
                        await ReceiveLoop(socket, lifetime.Token);
                    }
                    catch (OperationCanceledException) when (lifetime.IsCancellationRequested) { break; }
                    catch (Exception e)
                    {
                        if (ReferenceEquals(_lifetime, lifetime))
                            OnConnectionStatus?.Invoke($"connection_failed: {e.Message}");
                    }
                    finally
                    {
                        socket.Abort();
                        if (ReferenceEquals(_ws, socket)) _ws = null;
                    }
                    if (lifetime.IsCancellationRequested) break;
                    while (_inbox.TryDequeue(out _)) { }
                    OnConnectionStatus?.Invoke("disconnected");
                    if (started.Elapsed.TotalSeconds >= 10) attempt = 0;
                    int delay = ReconnectBackoffSeconds[Math.Min(attempt++, ReconnectBackoffSeconds.Length - 1)];
                    OnConnectionStatus?.Invoke($"reconnecting_in_{delay}s");
                    await Task.Delay(TimeSpan.FromSeconds(delay), lifetime.Token);
                }
            }
            catch (OperationCanceledException) { }
            finally
            {
                if (ReferenceEquals(_lifetime, lifetime)) _lifetime = null;
                lifetime.Dispose();
            }
        }

        // Existing POSITION is canonical: metres, +x Unity right, +y Unity forward,
        // heading zero +x, positive toward +y. Unity yaw is zero +z and turns toward +x.
        // Repeat can return exactly 360 for a tiny negative input (yaw a hair above 90), which the
        // engine rejects (heading is [0, 360)) and then drops every POSITION from this player.
        public static float HeadingFromUnityYaw(float yaw)
        {
            float heading = Mathf.Repeat(90f - yaw, 360f);
            return heading >= 360f ? 0f : heading;
        }

        public void SendUnityPosition(float x, float z, float yaw)
        {
            SendPosition(x, z, HeadingFromUnityYaw(yaw));
        }

        public async void SendPosition(float x, float y, float heading)
        {
            var socket = _ws;
            var lifetime = _lifetime;
            if (socket == null || lifetime == null || !IsConnected) return;
            // Never accumulate position sends behind a stalled tunnel.
            if (!await _sendLock.WaitAsync(0)) return;
            try
            {
                var position = new Position {
                    Type = "POSITION", Version = 1,
                    Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                    PlayerId = PlayerId, X = x, Y = y, Heading = heading,
                };
                byte[] bytes = Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(position));
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
                timeout.CancelAfter(TimeSpan.FromSeconds(3));
                await socket.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, timeout.Token);
            }
            catch (Exception e)
            {
                if (ReferenceEquals(_ws, socket)) {
                    Debug.LogWarning($"EngineClient send failed: {e.Message}");
                    socket.Abort(); // the owning receive loop detects this and reconnects
                }
            }
            finally { _sendLock.Release(); }
        }

        private async Task ReceiveLoop(ClientWebSocket socket, CancellationToken token)
        {
            var buffer = new byte[8192];
            while (!token.IsCancellationRequested)
            {
                using var stream = new System.IO.MemoryStream();
                WebSocketReceiveResult result;
                do
                {
                    result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), token);
                    if (result.MessageType == WebSocketMessageType.Close) return;
                    stream.Write(buffer, 0, result.Count);
                    if (stream.Length > 65536) throw new InvalidOperationException("Engine message too large");
                } while (!result.EndOfMessage);
                if (token.IsCancellationRequested) return;
                if (_inbox.Count >= 256) throw new InvalidOperationException("Engine inbox capacity exceeded");
                _inbox.Enqueue(Encoding.UTF8.GetString(stream.ToArray()));
            }
        }

        private void Update()
        {
            while (_inbox.TryDequeue(out string json)) DispatchMessage(json);
        }

        private void OnApplicationPause(bool paused)
        {
            if (paused) {
                _resumeOnForeground = _lifetime != null;
                Disconnect();
            } else if (_resumeOnForeground) {
                _resumeOnForeground = false;
                Connect();
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

            try
            {
                Dispatch(type, json);
            }
            catch (JsonException e)
            {
                // One bad payload must not abort the rest of this frame's inbox drain.
                Debug.LogWarning($"EngineClient: dropped malformed {type}: {e.Message}");
            }
        }

        private void Dispatch(string type, string json)
        {
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
            Disconnect();
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
