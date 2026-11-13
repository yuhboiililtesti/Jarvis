<#
.SYNOPSIS
Turn the JARVIS live screen reader on or off.

.EXAMPLE
capture off      # stop sampling and forget the last screen sample
capture on       # resume (starts the reader if it is not running)
capture status   # show whether capture is on
capture off -Stop  # pause and stop the reader process entirely
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("on", "off", "status")]
    [string]$Action = "status",
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) { $python = Join-Path $root ".venv\Scripts\python.exe" }
$base = "http://127.0.0.1:8799"
$headers = @{}
if ($env:JARVIS_SCREEN_TOKEN) {
    $headers["X-Jarvis-Screen-Token"] = $env:JARVIS_SCREEN_TOKEN
}

function Get-Health {
    try {
        return Invoke-RestMethod -Uri "$base/health" -TimeoutSec 3 -Headers $headers
    } catch {
        return $null
    }
}

function Wait-Health([int]$Attempts = 40) {
    for ($i = 0; $i -lt $Attempts; $i++) {
        $h = Get-Health
        if ($h) { return $h }
        Start-Sleep -Milliseconds 500
    }
    return $null
}

switch ($Action) {
    "status" {
        $h = Get-Health
        if (-not $h) {
            Write-Output "capture: off (reader not running)"
        } elseif ($h.capturing) {
            Write-Output ("capture: on  (samples={0}, errors={1}, interval={2}s)" -f `
                $h.samples, $h.errors, $h.interval_s)
        } else {
            Write-Output "capture: off (reader running, paused)"
        }
    }
    "off" {
        $h = Get-Health
        if (-not $h) {
            Write-Output "capture: already off (reader not running)"
            break
        }
        Invoke-RestMethod -Method Post -Uri "$base/capture/off" -TimeoutSec 5 `
            -Headers $headers | Out-Null
        Write-Output "capture: off"
        if ($Stop) {
            Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
                Where-Object { $_.CommandLine -match "jarvis\.screen_live" } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
            Write-Output "reader: stopped"
        }
    }
    "on" {
        $h = Get-Health
        if (-not $h) {
            Start-Process -WindowStyle Hidden -FilePath $python `
                -ArgumentList "-m", "jarvis.screen_live", "--interval", "5", "--start-paused" `
                -WorkingDirectory $root
            $h = Wait-Health
            if (-not $h) {
                throw "the reader did not start; run it in a console to see the error"
            }
        }
        Invoke-RestMethod -Method Post -Uri "$base/capture/on" -TimeoutSec 10 `
            -Headers $headers | Out-Null
        Write-Output "capture: on"
    }
}
