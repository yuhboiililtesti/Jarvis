<#
.SYNOPSIS
    Run the JARVIS NIGHT PROTOCOL (bounded unattended operation) now.

.DESCRIPTION
    Implements Documentation\Agents\NIGHT-PROTOCOL.md: preflight, recon, audit,
    bounded work loop, 3->2->1 retry ladder, health gates, reconciliation, and a
    morning report. It only performs work the policy allows and stops on unsafe
    uncertainty.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\jarvis-night.ps1 -Objective "verify tool registry and db integrity"
#>
[CmdletBinding()]
param(
    [string]$Objective = "",
    [int]$MaxMinutes = 0
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }

if ($MaxMinutes -gt 0) { $env:JARVIS_NIGHT__MAX_MINUTES = "$MaxMinutes" }

Write-Host "JARVIS night protocol starting at $(Get-Date -Format s)" -ForegroundColor Cyan
if ($Objective) {
    & $py -m jarvis night $Objective
} else {
    & $py -m jarvis night
}
$code = $LASTEXITCODE
Write-Host "Finished at $(Get-Date -Format s) (exit $code)"
Write-Host "Reports: data\night-reports\"
exit $code
