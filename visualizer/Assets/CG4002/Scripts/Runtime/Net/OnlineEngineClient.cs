using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using CG4002.Controllers;
using CG4002.State;
using UnityEngine;

namespace CG4002.Net
{
    /// <summary>Only network/queue ownership lives here. Unity state is applied on the main thread.</summary>
    [DefaultExecutionOrder(-200)]
    public class OnlineEngineClient : MonoBehaviour
    {
        public MessageDispatcher dispatcher;
        public GameState gameState;
        public AvatarController opponent;
        public SharedArenaCalibration calibration;
        public GameObject offlineEngine;
        [Tooltip("Base URL without /visualizer/id. WSS uses the platform's certificate trust.")]
        public string endpoint = "wss://localhost:8443";
        public PlayerId player = PlayerId.P1;
        [NonSerialized] public string bearerToken = "";

        public string Status { get; private set; } = "Enter connection settings";
        public bool Connected => socket != null && socket.State == WebSocketState.Open;
        readonly ConcurrentQueue<(int generation, string json)> inbox = new();
        readonly ConcurrentQueue<(int generation, string json)> outbox = new();
        CancellationTokenSource lifetime;
        ClientWebSocket socket;
        int generation;
        int observedGeneration = -1;
        bool? lastReady;
        bool foreground = true;
        string reconnectStatus = "";
        string lastSnapshot = "";
        float lastSnapshotAt;
        bool showSettings = true;
        PhoneSetupStatus setupStatus;
        bool showDiagnostics;
        int observedCalibration = -1;
        const float PanelWidth = 410;
        float PanelHeight => showDiagnostics ? 540 : 350;
        float PanelScale => Mathf.Max(0.5f, Mathf.Min(Screen.safeArea.width / (PanelWidth + 20),
            Screen.safeArea.height / (PanelHeight + 20)));
        Rect PanelRect => new Rect(Screen.safeArea.xMin / PanelScale + 10,
            (Screen.height - Screen.safeArea.yMax) / PanelScale + 10, PanelWidth, PanelHeight);
        static long Now => (long)(System.Diagnostics.Stopwatch.GetTimestamp() *
            (1000.0 / System.Diagnostics.Stopwatch.Frequency));

        void Awake()
        {
            if (offlineEngine != null) offlineEngine.SetActive(false);
            ConfigureIdentity();
        }

        void OnEnable()
        {
            if (calibration != null && calibration.placer != null)
                calibration.placer.BlockPlacementInput = PointerOverPanel;
        }

        void ConfigureIdentity()
        {
            gameState.ConfigureLocalPlayer(player);
            opponent.ConfigurePlayer(player == PlayerId.P1 ? PlayerId.P2 : PlayerId.P1);
        }

        public void Connect()
        {
            if (lifetime != null || string.IsNullOrWhiteSpace(bearerToken)) return;
            if (player != PlayerId.P1 && player != PlayerId.P2) { Status = "Choose P1 or P2"; return; }
            if (!Uri.TryCreate(endpoint, UriKind.Absolute, out var uri) ||
                (uri.Scheme != "ws" && uri.Scheme != "wss") || uri.AbsolutePath != "/" ||
                !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.UserInfo))
            { Status = "Use ws://host:port or wss://host:port"; return; }
            ConfigureIdentity();
            lifetime = new CancellationTokenSource();
            _ = Run(lifetime, endpoint.TrimEnd('/') + "/visualizer/" + (int)player, bearerToken);
            showSettings = false;
        }

        public void Disconnect()
        {
            var owner = lifetime;
            lifetime = null;
            owner?.Cancel();
            socket?.Abort();
            socket = null;
            Interlocked.Increment(ref generation);
            while (inbox.TryDequeue(out _)) { }
            while (outbox.TryDequeue(out _)) { }
            lastSnapshot = "";
            setupStatus = null;
            Status = "Disconnected";
            lastReady = null;
        }

        void OnDisable()
        {
            Disconnect();
            if (calibration != null && calibration.placer != null) calibration.placer.BlockPlacementInput = null;
        }

        bool PointerOverPanel(Vector2 point)
        {
            return PanelRect.Contains(new Vector2(point.x / PanelScale, (Screen.height - point.y) / PanelScale));
        }
        void OnApplicationPause(bool paused)
        {
            foreground = !paused;
            if (paused) { calibration.Invalidate(); socket?.Abort(); }
        }

        async Task Run(CancellationTokenSource owner, string url, string token)
        {
            int attempts = 0;
            try
            {
                while (!owner.IsCancellationRequested)
                {
                    if (!foreground) { await Task.Delay(250, owner.Token); continue; }
                    using var current = new ClientWebSocket();
                    using var session = CancellationTokenSource.CreateLinkedTokenSource(owner.Token);
                    int epoch = Interlocked.Increment(ref generation);
                    current.Options.SetRequestHeader("Authorization", "Bearer " + token);
                    current.Options.KeepAliveInterval = TimeSpan.FromSeconds(5);
                    try
                    {
                        using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(owner.Token))
                        {
                            timeout.CancelAfter(5000);
                            await current.ConnectAsync(new Uri(url), timeout.Token);
                        }
                        if (owner.IsCancellationRequested) break;
                        socket = current;
                        reconnectStatus = "Connected; waiting for state";
                        var receive = Receive(current, epoch, session.Token);
                        var send = Send(current, epoch, session.Token);
                        try { await await Task.WhenAny(receive, send); }
                        finally
                        {
                            session.Cancel(); current.Abort();
                            try { await Task.WhenAll(receive, send); } catch (Exception) { }
                        }
                    }
                    catch (OperationCanceledException) when (owner.IsCancellationRequested) { break; }
                    catch (Exception e)
                    {
                        if (ReferenceEquals(lifetime, owner)) reconnectStatus = "Reconnecting: " + e.GetType().Name;
                    }
                    finally
                    {
                        current.Abort();
                        if (ReferenceEquals(socket, current)) socket = null;
                    }
                    if (owner.IsCancellationRequested) break;
                    await Task.Delay(Math.Min(8000, 1000 * (1 << Math.Min(attempts++, 3))), owner.Token);
                }
            }
            catch (OperationCanceledException) { }
            finally { owner.Dispose(); }
        }

        async Task Receive(ClientWebSocket current, int epoch, CancellationToken cancel)
        {
            byte[] bytes = new byte[8192];
            while (!cancel.IsCancellationRequested)
            {
                using var message = new MemoryStream();
                WebSocketReceiveResult result;
                do
                {
                    using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancel);
                    timeout.CancelAfter(5000); // Online engine sends snapshots every 100 ms.
                    result = await current.ReceiveAsync(new ArraySegment<byte>(bytes), timeout.Token);
                    if (result.MessageType != WebSocketMessageType.Text) throw new IOException("Expected text");
                    message.Write(bytes, 0, result.Count);
                    if (message.Length > 65536) throw new IOException("Message exceeds limit");
                } while (!result.EndOfMessage);
                if (inbox.Count >= 64) throw new IOException("Inbound overflow; resnapshot");
                inbox.Enqueue((epoch, Encoding.UTF8.GetString(message.ToArray())));
            }
        }

        async Task Send(ClientWebSocket current, int epoch, CancellationToken cancel)
        {
            while (!cancel.IsCancellationRequested)
            {
                if (!outbox.TryDequeue(out var pending)) { await Task.Delay(10, cancel); continue; }
                if (pending.generation != epoch) continue;
                byte[] bytes = Encoding.UTF8.GetBytes(pending.json);
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancel);
                timeout.CancelAfter(3000);
                await current.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, timeout.Token);
            }
        }

        void Queue(string json)
        {
            if (!Connected) return;
            if (outbox.Count >= 32) { socket?.Abort(); return; }
            outbox.Enqueue((generation, json));
        }

        void Update()
        {
            int epoch = generation;
            if (observedGeneration != epoch)
            {
                observedGeneration = epoch; lastReady = null; lastSnapshot = "";
                setupStatus = null;
                lastSnapshotAt = Time.unscaledTime;
            }
            Status = Connected ? reconnectStatus : lifetime == null ? "Disconnected" : reconnectStatus;
            for (int i = 0; i < 32 && inbox.TryDequeue(out var incoming); i++)
            {
                if (incoming.generation != epoch || !Connected) continue;
                try
                {
                    var messages = EngineWireCodec.Decode(incoming.json);
                    foreach (var message in messages) dispatcher.Inbound.Enqueue(message);
                    if (incoming.json.Contains("\"STATE_SNAPSHOT\""))
                    {
                        lastSnapshot = incoming.json; lastSnapshotAt = Time.unscaledTime;
                        setupStatus = PhoneSetupStatus.Read(incoming.json);
                        reconnectStatus = "Connected";
                    }
                }
                catch (Exception e) { Status = "Invalid engine message: " + e.Message; }
            }
            if (Connected && Time.unscaledTime - lastSnapshotAt > 5f)
            { reconnectStatus = "No state snapshots; use engine.integration.yaml"; socket?.Abort(); }

            bool ready = foreground && calibration.IsReady;
            if (observedCalibration != calibration.Revision)
            {
                observedCalibration = calibration.Revision;
                // Positions queued before confirmation belong to the previous coordinate frame.
                while (dispatcher.Outbound.TryDequeue(out _)) { }
                if (Connected && lastReady == true) Queue(EngineWireCodec.Ready(player, false, Now));
                lastReady = null;
            }
            if (Connected && lastReady != ready)
            { Queue(EngineWireCodec.Ready(player, ready, Now)); lastReady = ready; }
            // Offline producers are disabled. Drop all stale outgoing data while disconnected.
            while (dispatcher.Outbound.TryDequeue(out var message))
            {
                if (message is PositionMessage position && ready && position.player == player)
                    Queue(EngineWireCodec.Position(player, position.position, position.headingDegrees,
                        position.monotonicTimestampMs >= 0 ? position.monotonicTimestampMs : Now));
                else if (message is StartMatchRequestMessage)
                {
                    // Placement/reset button requests display refresh, never global reset.
                    if (!string.IsNullOrEmpty(lastSnapshot))
                        foreach (var state in EngineWireCodec.Decode(lastSnapshot)) dispatcher.Inbound.Enqueue(state);
                    Status = "Match reset is controlled by the engine operator";
                }
            }
        }

        void OnGUI()
        {
            // Runtime-only token input: it is never serialized into a scene or PlayerPrefs.
            float scale = PanelScale;
            GUI.matrix = Matrix4x4.Scale(Vector3.one * scale);
            GUILayout.BeginArea(PanelRect, GUI.skin.box);
            GUILayout.Label("Online P" + (int)player + " · " + Status);
            GUILayout.Label("Match: " + gameState.Phase + " · Move cooldown: " +
                (gameState.Self.cooldownActionName ?? "none") + " " + gameState.Self.CooldownRemaining.ToString("0.0") + "s");
            if (lifetime == null || showSettings)
            {
                endpoint = GUILayout.TextField(endpoint);
                bearerToken = GUILayout.PasswordField(bearerToken, '*');
                player = GUILayout.SelectionGrid((int)player - 1, new[] { "Player 1", "Player 2" }, 2) == 0 ? PlayerId.P1 : PlayerId.P2;
                if (GUILayout.Button("Connect")) Connect();
            }
            else if (GUILayout.Button("Disconnect / settings")) { Disconnect(); showSettings = true; }
            GUILayout.Label("Tap the shared floor origin, then stand on the marked +Z line.");
            GUILayout.Label("Calibration: " + calibration.Status);
            if (GUILayout.Button(calibration.IsReady ? "Calibrated — reconfirm direction" : "Confirm shared +Z direction"))
                Status = calibration.ConfirmFromMarkedForwardLine() ? "Calibration confirmed" : "Place arena; stand at least 0.3 m from origin";
            if (GUILayout.Button(showDiagnostics ? "Hide setup readings" : "Show setup readings"))
                showDiagnostics = !showDiagnostics;
            if (showDiagnostics)
            {
                bool fresh = Connected && setupStatus != null && Time.unscaledTime - lastSnapshotAt < 1f;
                if (setupStatus == null) GUILayout.Label("Waiting for engine snapshot");
                else
                {
                    GUILayout.Label($"Snapshot age: {Time.unscaledTime - lastSnapshotAt:F1}s" +
                        (fresh ? "" : " - STALE / disconnected"));
                    GUILayout.Label(setupStatus.phase + (setupStatus.reason == null ? "" : " / " + setupStatus.reason));
                    GUILayout.Label(setupStatus.PlayerLine(0));
                    GUILayout.Label(setupStatus.PlayerLine(1));
                    GUILayout.Label(fresh && setupStatus.Separation.HasValue
                        ? $"Separation: {setupStatus.Separation.Value:F2} m" : "Separation unavailable");
                }
            }
            GUILayout.EndArea();
            GUI.matrix = Matrix4x4.identity;
        }
    }
}
