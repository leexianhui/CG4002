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
        [Tooltip("Optional. Opens after calibration, in the headset; the panel says so meanwhile.")]
        public CG4002.UI.WeaponSelectScreen weaponSelect;
        public GameObject offlineEngine;
        [Tooltip("Optional. Shows a DEMO MODE button: the whole game on this phone, no hardware or engine.")]
        public CG4002.Offline.DemoMode demoMode;
        [Tooltip("Base URL without /visualizer/id. 127.0.0.1:8080 is served by the built-in tunnel (or Termux socat).")]
        public string endpoint = "ws://127.0.0.1:8080";
        public PlayerId player = PlayerId.P1;
        [NonSerialized] public string bearerToken = "";
        // Replaces Termux ssh -J + socat; settings from Resources/ssh_tunnel.txt. Off in the editor by default.
#if UNITY_EDITOR
        [NonSerialized] public bool builtInTunnel = false;
#else
        [NonSerialized] public bool builtInTunnel = true;
#endif
        PhoneTunnel.Settings tunnelSettings = new();
        bool askTunnelPasswords = true;
        PhoneTunnel tunnel;

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
        // Collapsed: only a small "Online" button, so the HUD (health, weapon) is unobstructed.
        // Only the player collapses or reopens it.
        bool collapsed;
        int observedCalibration = -1;
        const float PanelWidth = 410;
        const float CollapsedWidth = 150, CollapsedHeight = 44;
        float PanelHeight => 540 + 50 + (askTunnelPasswords && builtInTunnel ? 56 : 0) + (demoMode != null ? 50 : 0);
        // Scale is fixed by the full panel so the collapsed button keeps the same size.
        float PanelScale => Mathf.Max(0.5f, Mathf.Min(Screen.safeArea.width / (PanelWidth + 20),
            Screen.safeArea.height / (PanelHeight + 20)));
        Rect PanelRect => new Rect(Screen.safeArea.xMin / PanelScale + 10,
            (Screen.height - Screen.safeArea.yMax) / PanelScale + 10,
            collapsed ? CollapsedWidth : PanelWidth, collapsed ? CollapsedHeight : PanelHeight);
        static long Now => (long)(System.Diagnostics.Stopwatch.GetTimestamp() *
            (1000.0 / System.Diagnostics.Stopwatch.Frequency));

        const string PlayerPref = "cg4002.player";
        [Tooltip("Fill the token from Resources/engine_token and the player from this phone's last choice.")]
        public bool loadSavedSettings = true;

        void Awake()
        {
            if (offlineEngine != null) offlineEngine.SetActive(false);
            if (loadSavedSettings) LoadSavedSettings();
            ConfigureIdentity();
        }

        // Token: Resources/engine_token.txt, kept in step with comms/.env.engine by EngineTokenBuildStep.
        // Player: the last choice made on this phone. Both stay editable in the settings panel.
        void LoadSavedSettings()
        {
            var baked = Resources.Load<TextAsset>("engine_token");
            if (string.IsNullOrWhiteSpace(bearerToken) && baked != null) bearerToken = baked.text.Trim();
            int saved = PlayerPrefs.GetInt(PlayerPref, 0);
            if (saved == 1 || saved == 2) player = (PlayerId)saved;
            if (!string.IsNullOrWhiteSpace(bearerToken)) Status = "Choose your player, then Connect";
            var ssh = Resources.Load<TextAsset>("ssh_tunnel");
            if (ssh != null) tunnelSettings = PhoneTunnel.Settings.Parse(ssh.text);
            askTunnelPasswords = !tunnelSettings.Complete;
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
            if (lifetime != null) return;
            if (string.IsNullOrWhiteSpace(bearerToken)) { Status = "Enter the engine token"; return; }
            if (player != PlayerId.P1 && player != PlayerId.P2) { Status = "Choose P1 or P2"; return; }
            if (!Uri.TryCreate(endpoint, UriKind.Absolute, out var uri) ||
                (uri.Scheme != "ws" && uri.Scheme != "wss") || uri.AbsolutePath != "/" ||
                !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.UserInfo))
            { Status = "Use ws://host:port or wss://host:port"; return; }
            bool loopback = uri.Host == "127.0.0.1" || uri.Host == "localhost";
            if (builtInTunnel && loopback)
            {
                if (!tunnelSettings.Complete) { Status = "Enter both SSH passwords"; return; }
                tunnel = new PhoneTunnel(tunnelSettings, uri.Port);
                tunnel.Start();
                if (!tunnel.Running) { Status = tunnel.Status; tunnel = null; return; }
            }
            ConfigureIdentity();
            if (loadSavedSettings) { PlayerPrefs.SetInt(PlayerPref, (int)player); PlayerPrefs.Save(); }
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
            tunnel?.Stop();
            tunnel = null;
            Interlocked.Increment(ref generation);
            while (inbox.TryDequeue(out _)) { }
            while (outbox.TryDequeue(out _)) { }
            lastSnapshot = "";
            setupStatus = null;
            Status = "Disconnected";
            lastReady = null;
        }

        /// <summary>
        /// RESTART: the operator's match reset (RESET_MATCH on /control, as
        /// comms/src/cg_comms/tools/reset_match.py sends it), with this phone's own token. The engine
        /// starts a fresh match (full HP; weapons stay, they are the hub's) and announces it to both
        /// phones, which then go back to dummy placement. This phone goes back straight away too, so
        /// it restarts even when the engine is out of reach.
        /// </summary>
        void Restart()
        {
            BackToPlacement();
            if (lifetime == null)
            {
                Status = "Not connected: restarted this phone only";
                return;
            }
            Status = "Restarting the match";
            // The engine confirms a reset on this socket once both phones have been told.
            _ = SendControl(EngineWireCodec.ResetMatch(), awaitReply: true,
                "Match reset: place the dummy again", "Restart not confirmed by the engine");
        }

        /// <summary>
        /// GOD MODE: asks the engine to switch god mode for both players (SET_GOD_MODE on /control).
        /// The button shows the engine's answer from its snapshots, so both phones agree.
        /// </summary>
        void ToggleGodMode()
        {
            if (lifetime == null)
            {
                Status = "Connect first: god mode is kept by the engine";
                return;
            }
            bool enable = !gameState.GodMode;
            Status = enable ? "Turning god mode on" : "Turning god mode off";
            _ = SendControl(EngineWireCodec.SetGodMode(enable), awaitReply: false,
                enable ? "God mode on: nobody loses HP" : "God mode off", "God mode not sent");
        }

        /// <summary>
        /// TUTORIAL / EXIT TUTORIAL: asks the engine to let <paramref name="who"/> practise alone against
        /// a dummy (null ends it). The button and the lessons follow the engine's snapshot.
        /// </summary>
        public void SetTutorial(PlayerId? who, bool dummyGuards)
        {
            if (lifetime == null)
            {
                Status = "Connect first: the tutorial runs on the engine";
                return;
            }
            Status = who == null ? "Ending the tutorial" : "Starting the tutorial";
            _ = SendControl(EngineWireCodec.SetTutorial(who, dummyGuards), awaitReply: false,
                who == null ? "Tutorial ended" : dummyGuards ? "Free practice" : "Tutorial on: practise alone",
                "Tutorial not sent");
        }

        /// <summary>Lessons done (TutorialController): the dummy starts guarding now and then.</summary>
        public void StartFreePractice() => SetTutorial(player, dummyGuards: true);

        /// <summary>One operator command on a short-lived /control socket, with this phone's token.</summary>
        async Task SendControl(string command, bool awaitReply, string done, string failed)
        {
            try
            {
                using var control = new ClientWebSocket();
                control.Options.SetRequestHeader("Authorization", "Bearer " + bearerToken);
                using var timeout = new CancellationTokenSource(5000);
                await control.ConnectAsync(new Uri(endpoint.TrimEnd('/') + "/control"), timeout.Token);
                var bytes = Encoding.UTF8.GetBytes(command);
                await control.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, timeout.Token);
                if (awaitReply)
                    await control.ReceiveAsync(new ArraySegment<byte>(new byte[4096]), timeout.Token);
                await control.CloseAsync(WebSocketCloseStatus.NormalClosure, "", timeout.Token);
                Status = done;
            }
            catch (Exception e) { Status = failed + ": " + e.GetType().Name; }
        }

        /// <summary>Back to the start of setup: the dummy is picked up, so calibration is cleared and this phone reports not ready.</summary>
        void BackToPlacement()
        {
            if (calibration != null && calibration.placer != null)
                calibration.placer.BeginPlacement();
        }

        /// <summary>Hands the game to demo mode (Docs/DemoMode.md): this client disconnects and turns off.</summary>
        void StartDemo()
        {
            Disconnect();
            ConfigureIdentity();  // the player chosen in the panel; the enemy is the other one
            calibration.enabled = false;  // one phone, no shared frame, and no headset prompts
            demoMode.Begin();
            enabled = false;
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
            // Backgrounded phones (iOS especially) kill the SSH sockets; rebuild on return.
            if (paused) tunnel?.Stop(); else tunnel?.Start();
        }

        async Task Run(CancellationTokenSource owner, string url, string token)
        {
            int attempts = 0;
            try
            {
                while (!owner.IsCancellationRequested)
                {
                    if (!foreground) { await Task.Delay(250, owner.Token); continue; }
                    var ssh = tunnel;
                    if (ssh != null && !ssh.Up)
                    {
                        reconnectStatus = "Waiting for SSH tunnel";
                        attempts = 0;
                        await Task.Delay(250, owner.Token);
                        continue;
                    }
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
                    if (incoming.json.Contains("\"MATCH_STATE\"") && EngineWireCodec.IsMatchReset(incoming.json))
                    {
                        BackToPlacement();
                        Status = "Match reset: place the dummy again";
                    }
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
            // The token is never serialized into a scene or PlayerPrefs (only the player choice is saved).
            float scale = PanelScale;
            GUI.matrix = Matrix4x4.Scale(Vector3.one * scale);
            if (collapsed)
            {
                GUILayout.BeginArea(PanelRect);
                if (GUILayout.Button("P" + (int)player + " · " + gameState.Phase, GUILayout.Height(CollapsedHeight)))
                    collapsed = false;
                GUILayout.EndArea();
                GUI.matrix = Matrix4x4.identity;
                return;
            }
            GUILayout.BeginArea(PanelRect, GUI.skin.box);
            if (GUILayout.Button("Hide panel")) collapsed = true;
            GUILayout.Label("Online P" + (int)player + " · " + Status);
            GUILayout.Label("Match: " + gameState.Phase + " · Move cooldown: " +
                (gameState.Self.cooldownActionName ?? "none") + " " + gameState.Self.CooldownRemaining.ToString("0.0") + "s");
            GUILayout.Label("SSH tunnel: " + (tunnel != null ? tunnel.Status : builtInTunnel ? "starts on Connect" : "external (Termux)"));
            if (lifetime == null || showSettings)
            {
                endpoint = GUILayout.TextField(endpoint);
                bearerToken = GUILayout.PasswordField(bearerToken, '*');
                builtInTunnel = GUILayout.Toggle(builtInTunnel, "Built-in SSH tunnel (no Termux needed)");
                if (builtInTunnel && askTunnelPasswords)
                {
                    // Typed passwords stay in memory only, like the token.
                    GUILayout.BeginHorizontal();
                    GUILayout.Label(tunnelSettings.jumpUser + "@jump", GUILayout.Width(150));
                    tunnelSettings.jumpPassword = GUILayout.PasswordField(tunnelSettings.jumpPassword, '*');
                    GUILayout.EndHorizontal();
                    GUILayout.BeginHorizontal();
                    GUILayout.Label(tunnelSettings.boardUser + "@board", GUILayout.Width(150));
                    tunnelSettings.boardPassword = GUILayout.PasswordField(tunnelSettings.boardPassword, '*');
                    GUILayout.EndHorizontal();
                }
                player = GUILayout.SelectionGrid((int)player - 1, new[] { "Player 1", "Player 2" }, 2) == 0 ? PlayerId.P1 : PlayerId.P2;
                if (GUILayout.Button("Connect")) Connect();
                if (demoMode != null && GUILayout.Button("DEMO MODE (no hardware)", GUILayout.Height(44))) StartDemo();
            }
            else if (GUILayout.Button("Disconnect / settings")) { Disconnect(); showSettings = true; }
            if (GUILayout.Button("RESTART (both re-place the dummy)")) Restart();
            if (GUILayout.Button(gameState.GodMode ? "GOD MODE: ON (nobody loses HP)" : "GOD MODE: OFF")) ToggleGodMode();
            var tutorial = gameState.TutorialPlayer;
            if (GUILayout.Button(tutorial == null ? "TUTORIAL (practise alone vs a dummy)"
                    : tutorial == player ? "EXIT TUTORIAL" : "EXIT P" + (int)tutorial.Value + "'s TUTORIAL"))
                SetTutorial(tutorial == null ? player : (PlayerId?)null, dummyGuards: false);
            if (weaponSelect != null && weaponSelect.IsSelecting)
                GUILayout.Label("Choose your weapon: copy a pose in front of you.");
            else
            {
                GUILayout.Label("Tap the floor where the opponent should stand, put on the headset, " +
                    "then look at the opponent and hold still for 3 s. The weapon is chosen after.");
                GUILayout.Label("Calibration: " + calibration.Status);
                if (GUILayout.Button(calibration.IsCalibrated ? "Calibrated — reconfirm direction" : "Confirm direction by hand"))
                    Status = calibration.ConfirmFromMarkedForwardLine() ? "Calibration confirmed" : "Place arena; stand at least 0.3 m from origin";
            }
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
            GUILayout.EndArea();
            GUI.matrix = Matrix4x4.identity;
        }
    }
}
