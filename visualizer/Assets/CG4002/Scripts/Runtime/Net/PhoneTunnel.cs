using System;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
using Renci.SshNet;
using Renci.SshNet.Common;

namespace CG4002.Net
{
    /// <summary>
    /// The two Termux commands, inside the app (so iPhones work too):
    ///   ssh -J jumpUser@jumpHost -L 8443:127.0.0.1:8443 boardUser@boardHost -N
    ///   socat TCP-LISTEN:{listenPort},fork OPENSSL:127.0.0.1:8443,verify=0
    /// The ws:// client keeps dialling 127.0.0.1:{listenPort} exactly as before.
    /// </summary>
    public sealed class PhoneTunnel
    {
        public sealed class Settings
        {
            public string jumpHost = "stujump.comp.nus.edu.sg", jumpUser = "", jumpPassword = "";
            public string boardHost = "makerslab-fpga-53.ddns.comp.nus.edu.sg", boardUser = "xilinx", boardPassword = "";
            public int enginePort = 8443;

            public bool Complete => !string.IsNullOrEmpty(jumpHost) && !string.IsNullOrEmpty(jumpUser) &&
                !string.IsNullOrEmpty(jumpPassword) && !string.IsNullOrEmpty(boardHost) &&
                !string.IsNullOrEmpty(boardUser) && !string.IsNullOrEmpty(boardPassword);

            /// <summary>key=value lines; blank lines and # comments are skipped.</summary>
            public static Settings Parse(string text)
            {
                var settings = new Settings();
                foreach (var raw in (text ?? "").Split('\n'))
                {
                    string line = raw.Trim();
                    int equals = line.IndexOf('=');
                    if (line.Length == 0 || line[0] == '#' || equals < 0) continue;
                    string value = line.Substring(equals + 1).Trim();
                    switch (line.Substring(0, equals).Trim())
                    {
                        case "jump_host": settings.jumpHost = value; break;
                        case "jump_user": settings.jumpUser = value; break;
                        case "jump_password": settings.jumpPassword = value; break;
                        case "board_host": settings.boardHost = value; break;
                        case "board_user": settings.boardUser = value; break;
                        case "board_password": settings.boardPassword = value; break;
                        case "engine_port": int.TryParse(value, out settings.enginePort); break;
                    }
                }
                return settings;
            }
        }

        public string Status { get; private set; } = "Off";
        public bool Up => forwardedPort != 0;
        public bool Running => lifetime != null;

        readonly Settings settings;
        readonly int listenPort;
        CancellationTokenSource lifetime;
        TcpListener listener;
        volatile int forwardedPort;

        public PhoneTunnel(Settings settings, int listenPort)
        {
            this.settings = settings;
            this.listenPort = listenPort;
        }

        public void Start()
        {
            if (lifetime != null) return;
            if (!settings.Complete) { Status = "Enter the SSH passwords"; return; }
            try
            {
                listener = new TcpListener(IPAddress.Loopback, listenPort);
                listener.Start();
            }
            catch (SocketException)
            {
                listener = null;
                Status = "Port " + listenPort + " busy: stop Termux socat, or untick built-in tunnel";
                return;
            }
            lifetime = new CancellationTokenSource();
            _ = Accept(listener, lifetime.Token);
            _ = Supervise(lifetime.Token);
        }

        public void Stop()
        {
            var owner = lifetime;
            lifetime = null;
            owner?.Cancel();
            listener?.Stop();
            listener = null;
            forwardedPort = 0;
            Status = "Off";
        }

        async Task Supervise(CancellationToken cancel)
        {
            int attempts = 0;
            while (!cancel.IsCancellationRequested)
            {
                SshClient jump = null, board = null;
                string stage = settings.jumpHost;
                try
                {
                    Status = "Connecting to " + settings.jumpHost;
                    jump = Open(settings.jumpHost, 22, settings.jumpUser, settings.jumpPassword);
                    await jump.ConnectAsync(cancel);
                    // -J: a local port on the phone that comes out at the board's sshd.
                    var hop = new ForwardedPortLocal("127.0.0.1", 0, settings.boardHost, 22);
                    jump.AddForwardedPort(hop);
                    hop.Start();

                    stage = settings.boardHost;
                    Status = "Connecting to " + settings.boardHost;
                    board = Open("127.0.0.1", (int)hop.BoundPort, settings.boardUser, settings.boardPassword);
                    await board.ConnectAsync(cancel);
                    var engine = new ForwardedPortLocal("127.0.0.1", 0, "127.0.0.1", (uint)settings.enginePort);
                    board.AddForwardedPort(engine);
                    engine.Start();

                    cancel.ThrowIfCancellationRequested(); // a stopped session must not publish its port
                    forwardedPort = (int)engine.BoundPort;
                    attempts = 0;
                    Status = "Up";
                    while (!cancel.IsCancellationRequested && jump.IsConnected && board.IsConnected && engine.IsStarted)
                        await Task.Delay(1000, cancel);
                    if (!cancel.IsCancellationRequested) Status = "Dropped; reconnecting";
                }
                catch (OperationCanceledException) when (cancel.IsCancellationRequested) { }
                catch (Exception e) { if (!cancel.IsCancellationRequested) Status = Describe(e, stage); }
                finally
                {
                    if (!cancel.IsCancellationRequested) forwardedPort = 0; // else Stop() cleared it; a restart may own it now
                    try { board?.Dispose(); } catch (Exception) { }
                    try { jump?.Dispose(); } catch (Exception) { }
                }
                if (cancel.IsCancellationRequested) break;
                try { await Task.Delay(Math.Min(8000, 1000 * (1 << Math.Min(attempts++, 3))), cancel); }
                catch (OperationCanceledException) { break; }
            }
        }

        static SshClient Open(string host, int port, string user, string password)
        {
            // Some sshd configs only offer keyboard-interactive for passwords; answer every prompt with it.
            var keyboard = new KeyboardInteractiveAuthenticationMethod(user);
            keyboard.AuthenticationPrompt += (_, e) => { foreach (var prompt in e.Prompts) prompt.Response = password; };
            var info = new ConnectionInfo(host, port, user, new PasswordAuthenticationMethod(user, password), keyboard)
            { Timeout = TimeSpan.FromSeconds(10) };
            return new SshClient(info) { KeepAliveInterval = TimeSpan.FromSeconds(10) };
        }

        static string Describe(Exception e, string stage)
        {
            switch (e)
            {
                case SshAuthenticationException _: return "Wrong user/password for " + stage;
                case SshOperationTimeoutException _: return "Timed out reaching " + stage;
                case SocketException _: return "Can't reach " + stage + " (network/VPN?)";
                default: return stage + ": " + e.GetType().Name + " " + e.Message;
            }
        }

        // socat's half: plain TCP from the app in, TLS to the engine out through the SSH forward.
        async Task Accept(TcpListener server, CancellationToken cancel)
        {
            while (!cancel.IsCancellationRequested)
            {
                TcpClient app;
                try { app = await server.AcceptTcpClientAsync(); }
                catch (Exception) { return; } // listener stopped
                _ = Bridge(app, cancel);
            }
        }

        async Task Bridge(TcpClient app, CancellationToken cancel)
        {
            using (app)
            using (var engine = new TcpClient())
            {
                int port = forwardedPort;
                if (port == 0) return; // tunnel not up yet; the ws client sees a closed socket and retries
                try
                {
                    await engine.ConnectAsync(IPAddress.Loopback, port);
                    // verify=0 like socat: the engine cert is self-signed and the path is already inside SSH.
                    using var tls = new SslStream(engine.GetStream(), false, (_, _, _, _) => true);
                    await tls.AuthenticateAsClientAsync("localhost");
                    using var stop = cancel.Register(() => { app.Close(); engine.Close(); });
                    var plain = app.GetStream();
                    await Task.WhenAny(plain.CopyToAsync(tls, 8192, cancel), tls.CopyToAsync(plain, 8192, cancel));
                }
                catch (Exception) { }
            }
        }
    }
}
