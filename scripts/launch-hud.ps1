<#
.SYNOPSIS
    Start the JARVIS Core hidden (if needed) and open the HUD in the browser.

.DESCRIPTION
    The visible console was the bug: the old launcher opened the browser before the
    server was listening and then ran uvicorn in the foreground. This waits for the
    port, then opens the HUD. If the Core fails to start, it shows the log tail in a
    message box instead of leaving a dead browser tab.

    Safe to run when the Core is already up: start-core.ps1 detects the listening
    port and returns success, so the browser is simply opened.
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [switch]$Lan
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Starter = Join-Path $Root "scripts\cloud\start-core.ps1"
$Url = "http://127.0.0.1:$Port/"

function Show-Error([string]$text) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            $text, "JARVIS could not start", "OK", "Error") | Out-Null
    } catch {
        Write-Host $text
    }
}

if (-not (Test-Path $Starter)) {
    Show-Error "Starter not found:`n$Starter"
    exit 1
}

$args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Starter, "-Port", "$Port")
if ($Lan) { $args += "-Lan" }
& powershell @args
if ($LASTEXITCODE -ne 0) {
    $log = Join-Path $Root "data\logs\core-stderr.log"
    $tail = if (Test-Path $log) {
        (Get-Content $log -Tail 25) -join "`n"
    } else { "(no log written)" }
    Show-Error "The Core did not start.`n`n$tail`n`nFull logs: data\logs\"
    exit 1
}

# start-core.ps1 only returns success once something is listening on the port.
Start-Process $Url
exit 0
