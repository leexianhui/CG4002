# Tests, in order, every hop between this laptop and the Ultra96, and says which one is broken.
#
# Run it after changing anything about the network -- toggling a VPN, switching Wi-Fi, having IT
# adjust a policy -- instead of re-deriving the diagnosis by hand:
#
#   .\scripts\check_ultra96_reach.ps1
#
# WHY THIS EXISTS: "can't reach the board" has at least four distinct causes with the same symptom
# (a timeout), and they have completely different fixes. Answering "is it my Wi-Fi, is it NUS, is it
# Zscaler, or is it the board" by hand takes ten minutes and is easy to get wrong -- a `ping` that
# fails proves almost nothing on its own, because ICMP is filtered in places TCP is not. This runs
# the whole ladder and names the first rung that fails.
#
# As of 2026-09-10 on the work laptop the expected result is FAIL at step 4 (NUS will not route from
# the Wi-Fi subnet to the lab subnet) -- see DEMO_RUNBOOK.md section 7.0. That is not a bug to fix
# here; it is why deployment happens from the second laptop.

$ErrorActionPreference = 'Continue'

$BoardIp   = '172.26.191.151'
$BoardHost = 'makerslab-fpga-53.ddns.comp.nus.edu.sg'
$JumpHost  = 'stujump.comp.nus.edu.sg'

function Test-Tcp {
    param([string]$Target, [int]$Port, [int]$TimeoutMs = 5000)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($Target, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $client.Connected
    }
    catch { return $false }
    finally { $client.Close() }
}

function Say {
    param([bool]$Ok, [string]$Label, [string]$Detail = '')
    $mark = if ($Ok) { 'PASS' } else { 'FAIL' }
    $color = if ($Ok) { 'Green' } else { 'Red' }
    Write-Host ('  [{0}] {1}' -f $mark, $Label) -ForegroundColor $color
    if ($Detail) { Write-Host ("         $Detail") -ForegroundColor DarkGray }
}

Write-Host "`nUltra96 reachability ladder`n" -ForegroundColor Cyan

# --- 1. Are we on school Wi-Fi at all? The lab subnet is only routable from an NUS-issued address,
# --- so a corporate-tunnelled or hotspot address fails everything below for an uninteresting reason.
$wifi = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceAlias -eq 'Wi-Fi' -and $_.PrefixOrigin -eq 'Dhcp' } |
    Select-Object -First 1
$onNus = $wifi -and $wifi.IPAddress -match '^172\.2[0-9]\.'
Say $onNus 'On an NUS Wi-Fi address' $(if ($wifi) { "Wi-Fi = $($wifi.IPAddress)" } else { 'no DHCP address on Wi-Fi' })

# --- 2. Basic internet. Separates "the network is down" from "this specific route is blocked".
$net = Test-Tcp 'www.google.com' 443
Say $net 'Internet works (443 to a public host)'

# --- 3. DNS. The board's DDNS name must resolve, and it should resolve to a PRIVATE address --
# --- if it ever resolves to something public, the whole analysis below changes.
$resolved = $null
try { $resolved = (Resolve-DnsName $BoardHost -Type A -ErrorAction Stop | Select-Object -First 1).IPAddress } catch {}
Say ([bool]$resolved) "DNS resolves $BoardHost" $(if ($resolved) { "-> $resolved (private, so not reachable from the internet)" } else { 'no A record' })

# --- 4. The one that actually matters. TCP to the board's SSH port. ICMP is deliberately not the
# --- test here: ping being blocked would not stop SSH, and ping succeeding would not prove SSH works.
$board22 = Test-Tcp $BoardIp 22
Say $board22 "TCP $BoardIp`:22 (board SSH)"

# --- 5. The jump host, on its public address. If step 4 fails but this passes, you can tunnel.
# --- If both fail, there is no path at all from this machine.
$jump22 = Test-Tcp $JumpHost 22
Say $jump22 "TCP $JumpHost`:22 (jump host SSH)"

# --- 6. Is outbound SSH blocked as a PROTOCOL, independent of NUS? github.com:22 is a neutral
# --- control: if that fails too, something on this laptop is blocking port 22 everywhere, and no
# --- amount of NUS-side fixing will help.
$gh22 = Test-Tcp 'github.com' 22
Say $gh22 'TCP github.com:22 (control: is port 22 blocked everywhere?)'

# --- 7. Name the local security agent if present, since it explains step 6 and is not something
# --- you can configure around from user-land.
$zs = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like 'ZSA*' }
if ($zs) {
    Write-Host "`n  Note: Zscaler is running ($(($zs | Select-Object -ExpandProperty ProcessName -Unique) -join ', '))." -ForegroundColor Yellow
    Write-Host '         It intercepts outbound traffic below the VPN, so "turning off the VPN" does not' -ForegroundColor DarkGray
    Write-Host '         disable it. Only IT can exempt a destination.' -ForegroundColor DarkGray
}

# --- Verdict. Each branch has exactly one recommended action, because the failure identifies it.
Write-Host "`nVerdict:" -ForegroundColor Cyan
if ($board22) {
    Write-Host '  The board is REACHABLE from this laptop. Stage 3 can run here.' -ForegroundColor Green
    Write-Host "  ssh xilinx@$BoardIp" -ForegroundColor Gray
}
elseif ($jump22) {
    Write-Host '  Board unreachable directly, but the jump host answers -- go through it:' -ForegroundColor Yellow
    Write-Host "  ssh -J e1138443@$JumpHost xilinx@$BoardHost" -ForegroundColor Gray
}
elseif (-not $gh22) {
    Write-Host '  Port 22 is blocked to EVERY host, not just NUS. That is this laptop''s own security' -ForegroundColor Yellow
    Write-Host '  agent, not the school. Nothing you can change in Windows settings fixes it.' -ForegroundColor Yellow
    Write-Host '  -> Deploy from the second laptop. See DEMO_RUNBOOK.md section 7.0.' -ForegroundColor Gray
}
else {
    Write-Host '  Outbound SSH works in general, but not to NUS. The school does not route your Wi-Fi' -ForegroundColor Yellow
    Write-Host '  subnet to the lab subnet without the NUS VPN.' -ForegroundColor Yellow
    Write-Host '  -> Deploy from the second laptop. See DEMO_RUNBOOK.md section 7.0.' -ForegroundColor Gray
}
Write-Host ''
