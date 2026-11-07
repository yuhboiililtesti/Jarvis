<#
.SYNOPSIS
    Install JARVIS on the Windows Core host (workstation).

.DESCRIPTION
    Creates a virtual environment, installs the Core requirements, optionally the
    voice/compute stack, optionally pulls the local model, writes config and runs
    a diagnosis. Idempotent: safe to re-run. It changes nothing outside the
    project directory and the Ollama model store.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Voice -Model
#>
[CmdletBinding()]
param(
    [switch]$Voice,                       # install faster-whisper / piper / sounddevice etc.
    [switch]$Model,                       # pull the configured Ollama model
    [string]$ModelName = "qwen2.5:7b-instruct",
    [switch]$Force                        # recreate the venv
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }

Step "Checking Python"
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { throw "Python 3.11+ not found on PATH." }
$ver = (& python -c "import sys;print('%d.%d'%sys.version_info[:2])")
Write-Host "Python $ver at $($py.Source)"
if ([version]$ver -lt [version]"3.11") { throw "Python 3.11+ required (found $ver)." }

Step "Virtual environment"
if ($Force -and (Test-Path ".venv")) { Remove-Item -Recurse -Force ".venv" }
if (-not (Test-Path ".venv")) {
    & python -m venv .venv
    Write-Host "created .venv"
} else {
    Write-Host ".venv already exists"
}
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"

Step "Installing Core requirements"
& $venvPy -m pip install --upgrade pip wheel | Out-Null
& $venvPy -m pip install -r requirements.txt

if ($Voice) {
    Step "Installing voice + compute stack (large download)"
    & $venvPy -m pip install -r requirements-voice.txt
} else {
    Write-Host "`n(skipping voice stack; re-run with -Voice to install it)"
}

Step "Configuration"
if (-not (Test-Path "config\jarvis.toml")) {
    Copy-Item "config\jarvis.example.toml" "config\jarvis.toml"
    Write-Host "wrote config\jarvis.toml"
} else {
    Write-Host "config\jarvis.toml already exists (left untouched)"
}
& $venvPy -m jarvis init | Out-Null

if ($Model) {
    Step "Local model"
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        Write-Warning "ollama not found; install it from https://ollama.com then run: ollama pull $ModelName"
    } else {
        Write-Host "pulling $ModelName (several GB; resumable)..."
        & ollama pull $ModelName
    }
}

Step "Diagnosis"
& $venvPy -m jarvis doctor

Write-Host "`nDone." -ForegroundColor Green
Write-Host "Start the Core with:  .\scripts\run.cmd     then open http://127.0.0.1:8765/"
