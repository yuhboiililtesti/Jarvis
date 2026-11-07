<#
.SYNOPSIS
    Maintain an SSH tunnel from the Core to a satellite node's agent port.

.DESCRIPTION
    The ThinkPad runs `ufw`, which blocks the node's inbound port (2226) while
    allowing SSH (2225). Rather than opening a firewall hole (which needs root on
    the laptop), this forwards the node port over the SSH connection we already
    trust. Nothing is exposed on the LAN beyond SSH, and the agent is only
    reachable by whoever holds the SSH key.

        Core:127.0.0.1:2226  --ssh-->  laptop:127.0.0.1:2226 (jarvis-node)

    So a node is enrolled with base_url http://127.0.0.1:2226 once the tunnel is
    up. If you ever prefer a direct LAN connection, allow the port on the laptop:
        sudo ufw allow from 10.0.0.0/24 to any port 2226 proto tcp

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\node-tunnel.ps1 -Start
    powershell -ExecutionPolicy Bypass -File scripts\node-tunnel.ps1 -Status
    powershell -ExecutionPolicy Bypass -File scripts\node-tunnel.ps1 -Stop
#>
[CmdletBinding()]
param(
    [switch]$Start,
    [switch]$Stop,
    [switch]$Status,
    [string]$SshHost = "laptop",
    [int]$LocalPort = 2226,
    [int]$RemotePort = 2226
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $Root "data\node-tunnel.pid"
$logFile = Join-Path $Root "data\node-tunnel.log"
$outFile = Join-Path $Root "data\node-tunnel.out"

function Test-Tunnel {
    $c = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    return [bool]$c
}

if ($Stop) {
    if (Test-Path $pidFile) {
        $old = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($old) { Stop-Process -Id ([int]$old) -Force -ErrorAction SilentlyContinue }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
        Where-Object { $_.CommandLine -match "\-L\s+.*$LocalPort" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "tunnel stopped"
    exit 0
}

if ($Status -or (-not $Start)) {
    $up = Test-Tunnel
    Write-Host ("tunnel to {0}: local 127.0.0.1:{1} -> {2}:{3} : {4}" -f `
        $SshHost, $LocalPort, $SshHost, $RemotePort, $(if ($up) { "UP" } else { "DOWN" }))
    exit $(if ($up) { 0 } else { 1 })
}

if (Test-Tunnel) { Write-Host "tunnel already up on 127.0.0.1:$LocalPort"; exit 0 }

Write-Host "opening tunnel 127.0.0.1:$LocalPort -> ${SshHost}:$RemotePort ..."
$args = @(
    "-N",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-L", "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}",
    $SshHost
)
$p = Start-Process -FilePath "ssh" -ArgumentList $args -PassThru -WindowStyle Hidden `
    -RedirectStandardError $logFile -RedirectStandardOutput $outFile
Set-Content -Path $pidFile -Value $p.Id -Encoding ascii

# TCP forward needs the connection established; the listener may take a moment.
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 400
    if (Test-Tunnel) { break }
    if ($p.HasExited) { break }
}
if (Test-Tunnel) {
    Write-Host "tunnel UP (pid $($p.Id)) - enrol nodes at http://127.0.0.1:$LocalPort"
    exit 0
}
Write-Warning "tunnel did not come up. See $logFile"
if (Test-Path $logFile) { Get-Content $logFile -Tail 10 }
exit 1
