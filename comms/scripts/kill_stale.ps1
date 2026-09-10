# Kills leftover demo processes from this checkout. run_demo.sh does this automatically before every
# run, so you rarely need this -- it is here for when you started components by hand, or want to
# confirm a clean slate before flashing/measuring.
#
# Usage (from comms/):  .\scripts\kill_stale.ps1
#
# Why command-line matching rather than ports: cg-relay is a pure TCP *client* and holds no listening
# socket, so a port scan cannot see it -- yet an orphaned relay is the most disruptive leftover of
# all. It occupies the device sim's accept slot, so the next run's relay is refused with
# "[WinError 1225] The remote computer refused the network connection" and backs off to 10s, which
# looks exactly like a wrong start order or a broken rig.
#
# /T is required because .venv\Scripts\cg-*.exe launches python.exe as a child: killing the wrapper
# alone leaves the real process running and still holding its socket.

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

$stale = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*$root*" -and $_.CommandLine -like '*\Scripts\cg-*' }

if (-not $stale) {
    Write-Host "no stale cg_comms processes in $root"
}
else {
    foreach ($p in $stale) {
        # The entry-point name is the only human-meaningful part of these command lines.
        $name = if ($p.CommandLine -match 'cg-([a-z0-9-]+)\.exe') { $Matches[1] } else { 'unknown' }
        Write-Host ("killing {0,-16} pid {1}" -f $name, $p.ProcessId)
        # Stop-Process rather than taskkill: /T already kills children, so a PID later in this list is
        # often already gone, and taskkill writes "process not found" to *stderr*, which under
        # ErrorActionPreference='Stop' aborts the whole loop and leaves the rest alive. Failing to kill
        # an already-dead process is the expected case here, not an error.
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    Write-Host "killed $($stale.Count) process(es)"
}

# Ports are not how stale processes are found, but they ARE what the next run needs, so report them:
# a port still held here means something outside this checkout owns it (another checkout, or an
# unrelated service), which needs a human decision rather than a kill.
$held = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 8443, 8888, 9001 }
if ($held) {
    Write-Warning "demo ports still bound by processes outside this checkout:"
    $held | ForEach-Object {
        $owner = (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        Write-Host ("  port {0} <- pid {1} ({2})" -f $_.LocalPort, $_.OwningProcess, $owner)
    }
}
else {
    Write-Host "demo ports 8443/8888/9001 are free"
}
