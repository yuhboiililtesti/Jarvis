<#
.SYNOPSIS
    Start the JARVIS Core in the background with output logged to data\logs.

.DESCRIPTION
    Wrapper used by the per-user logon task (scripts\install-autostart.ps1). It can
    also be run by hand. It refuses to start a second Core when the port is
    already listening, so a manual start and the logon task cannot collide.

        data\logs\core-autostart.log   - this wrapper's own decisions
        data\logs\core-stdout.log      - Core stdout (uvicorn access + banner)
        data\logs\core-stderr.log      - Core stderr (application log)

    Stop it with:  scripts\cloud\start-core.ps1 -Stop   (or end python.exe in Task Manager)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\cloud\start-core.ps1
    powershell -ExecutionPolicy Bypass -File scripts\cloud\start-core.ps1 -Lan
    powershell -ExecutionPolicy Bypass -File scripts\cloud\start-core.ps1 -Stop
    powershell -ExecutionPolicy Bypass -File scripts\cloud\start-core.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$Lan,          # private-cloud mode: python -m jarvis serve --lan (token gate on)
    [int]$Port = 8765,
    [switch]$Stop,
    [switch]$DryRun        # print what would happen; start nothing
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$LogDir = Join-Path $Root "data\logs"
$PidFile = Join-Path $Root "data\core.pid"
$WrapLog = Join-Path $LogDir "core-autostart.log"
$Py = Join-Path $Root ".venv\Scripts\python.exe"

function Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss"), $msg
    Write-Host $line
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        Add-Content -Path $WrapLog -Value $line -Encoding utf8
    }
}

function Test-Listening([int]$p) {
    return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}

if ($Stop) {
    if (Test-Path $PidFile) {
        $old = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($old -and -not $DryRun) {
            Stop-Process -Id ([int]$old) -ErrorAction SilentlyContinue
            Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        }
        Log "stop requested for pid $old$(if ($DryRun) { ' (dry run: nothing stopped)' })"
    } else {
        Log "no pid file at $PidFile; nothing to stop"
    }
    # If a child interpreter still owns the port, stop only a python listener.
    Start-Sleep -Milliseconds 700
    if (Test-Listening $Port) {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -like "python*") {
                if (-not $DryRun) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
                Log "stopped leftover python listener pid $($proc.Id)$(if ($DryRun) { ' (dry run)' })"
            } else {
                Log "port $Port is owned by '$($proc.ProcessName)'; not killing it"
            }
        }
    }
    exit 0
}

if (-not (Test-Path $Py)) { Log "ERROR: $Py not found; run scripts\install.ps1 first"; exit 2 }
if (Test-Listening $Port) { Log "port $Port already listening; Core (or something else) is up - not starting another"; exit 0 }

$argList = @("-m", "jarvis", "serve", "--port", "$Port")
if ($Lan) { $argList += "--lan" }
Log ("starting: {0} {1} (cwd {2})" -f $Py, ($argList -join " "), $Root)
if ($DryRun) { Log "dry run: not started"; exit 0 }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$p = Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory $Root -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "core-stdout.log") `
    -RedirectStandardError (Join-Path $LogDir "core-stderr.log")
Set-Content -Path $PidFile -Value $p.Id -Encoding ascii

for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-Listening $Port) { Log "Core listening on port $Port (pid $($p.Id))"; exit 0 }
    if ($p.HasExited) { break }
}
Log "WARNING: Core did not start listening on $Port (exit=$($p.ExitCode)); see data\logs\core-stderr.log"
exit 1
