# Install the `capture` command for the current user.
#
# Copies a small wrapper into %USERPROFILE%\bin and adds that directory to the
# user PATH. Open a new terminal afterwards (or run the full path).
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\screen-capture.ps1"
if (-not (Test-Path $script)) {
    throw "screen-capture.ps1 not found at $script"
}

$bin = Join-Path $env:USERPROFILE "bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$target = Join-Path $bin "capture.cmd"
$content = @(
    "@echo off",
    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`" %*"
)
Set-Content -Path $target -Value $content -Encoding ascii

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$bin*") {
    [Environment]::SetEnvironmentVariable(
        "Path", ($userPath.TrimEnd(';') + ";" + $bin), "User")
    $pathNote = "user PATH updated"
} else {
    $pathNote = "user PATH already contains it"
}
Write-Output "installed $target ($pathNote); open a new terminal"
Write-Output "try: capture status"
