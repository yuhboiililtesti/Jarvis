# Register the screen reader at logon.
#
# It runs under pythonw.exe (no console window) and starts PAUSED: the service
# is available instantly, but no screen is read until the owner runs
# `capture on`. Nothing is recorded either way.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    throw "pythonw.exe not found at $python"
}

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m jarvis.screen_live --interval 5 --start-paused" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -Hidden `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "JARVIS Screen Reader" -Action $action `
    -Trigger $trigger -Settings $settings -Force `
    -Description "Live in-memory screen reader (hidden; starts paused; 'capture on|off')" | Out-Null

Write-Output "registered 'JARVIS Screen Reader' (hidden, starts paused)"
