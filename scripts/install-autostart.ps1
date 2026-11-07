<#
.SYNOPSIS
    Start the JARVIS Core automatically when you log on (per-user Scheduled Task).

.DESCRIPTION
    Registers ONE per-user scheduled task, "JARVIS Core", triggered at YOUR logon,
    running with your normal (non-elevated) rights. It runs
    scripts\cloud\start-core.ps1, which logs to data\logs\ and refuses to start a
    second Core if the port is already in use. No service, no admin rights, no
    firewall change.

    Disable / roll back (any one of these):
        powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall
        Disable-ScheduledTask -TaskName "JARVIS Core"        # keep it, but off
        Task Scheduler > Task Scheduler Library > "JARVIS Core" > Disable/Delete

    Always try -DryRun first: it prints the exact task definition and changes nothing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -DryRun
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Lan
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Status
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Lan,                       # start in private-cloud mode (token gate for LAN clients)
    [int]$Port = 8765,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$DryRun,                    # same as -WhatIf, plus prints the full definition
    [string]$TaskName = "JARVIS Core"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Starter = Join-Path $Root "scripts\cloud\start-core.ps1"
$LogDir = Join-Path $Root "data\logs"
$LogFile = Join-Path $LogDir "autostart-install.log"
if ($DryRun) { $WhatIfPreference = $true }

function Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss"), $msg
    Write-Host $line
    if (-not $WhatIfPreference) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    }
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Status) {
    if ($existing) {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host ("task '{0}': state={1} lastRun={2} lastResult={3}" -f `
            $TaskName, $existing.State, $info.LastRunTime, $info.LastTaskResult)
        Write-Host ("action: {0} {1}" -f $existing.Actions[0].Execute, $existing.Actions[0].Arguments)
    } else {
        Write-Host "task '$TaskName' is not registered"
    }
    exit 0
}

if ($Uninstall) {
    if (-not $existing) { Log "task '$TaskName' not registered; nothing to remove"; exit 0 }
    if ($PSCmdlet.ShouldProcess($TaskName, "Unregister-ScheduledTask")) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Log "removed task '$TaskName' (a Core that is already running keeps running; stop it with scripts\cloud\start-core.ps1 -Stop)"
    }
    exit 0
}

if (-not (Test-Path $Starter)) { throw "missing $Starter" }
$user = "$env:USERDOMAIN\$env:USERNAME"
$psArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Starter`" -Port $Port"
if ($Lan) { $psArgs += " -Lan" }

Write-Host "Planned scheduled task:"
Write-Host "  name      : $TaskName"
Write-Host "  trigger   : at logon of $user"
Write-Host "  run as    : $user (interactive, limited rights - no elevation)"
Write-Host "  action    : powershell.exe $psArgs"
Write-Host "  settings  : no time limit, restart 3x at 1 min, ignore a second instance, runs on battery"
Write-Host "  logs      : $LogDir\core-autostart.log, core-stdout.log, core-stderr.log"
Write-Host "  rollback  : scripts\install-autostart.ps1 -Uninstall"

if ($existing) { Write-Host "  note      : a task with this name exists and would be REPLACED" }

if ($PSCmdlet.ShouldProcess($TaskName, "Register-ScheduledTask (per-user, at logon)")) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description "Starts the JARVIS Core at logon. Remove: scripts\install-autostart.ps1 -Uninstall" `
        -Force | Out-Null
    Log "registered task '$TaskName' (lan=$([bool]$Lan), port=$Port). Verify: scripts\install-autostart.ps1 -Status"
} else {
    Write-Host "dry run: nothing registered"
}
