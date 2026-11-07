<#
.SYNOPSIS
  Fetch the local TTS voice models JARVIS uses, with size + SHA-256 checks.

.DESCRIPTION
  Downloads (only when missing or corrupt):
    * Kokoro v1.0 ONNX model + voice pack  -> data\voices\kokoro\
      source: https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0
    * Piper en_GB-alan-medium (fallback)  -> data\voices\
      source: https://huggingface.co/rhasspy/piper-voices (official Piper voice repo)

  Every file is downloaded to "<name>.part", checked against the expected byte
  size and SHA-256, and only then renamed into place. A mismatch deletes the
  partial file and exits non-zero. Re-running is idempotent: files that already
  match are left untouched and reported "ok".

  Hash provenance:
    * Piper alan: SHA-256 equals the Hugging Face LFS ETag (X-Linked-ETag), i.e.
      the hash the upstream publishes.
    * Kokoro: GitHub publishes the asset SIZE but no digest for this release, so
      the SHA-256 values below were recorded from the first verified download on
      2026-10-18 (trust-on-first-use) and pin every later download to it.

.PARAMETER DataDir
  Project data directory (default: <repo>\data).

.PARAMETER SkipPiper
  Do not check/fetch the Piper fallback voice.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\fetch-voices.ps1
#>
[CmdletBinding()]
param(
  [string]$DataDir = "",
  [switch]$SkipPiper
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with progress on

$Root = Split-Path -Parent $PSScriptRoot
if (-not $DataDir) { $DataDir = Join-Path $Root "data" }
$Voices = Join-Path $DataDir "voices"
$Kokoro = Join-Path $Voices "kokoro"

$KokoroBase = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
$PiperBase  = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium"

$Files = @(
  @{ Name = "kokoro-v1.0.onnx"; Dir = $Kokoro; Url = "$KokoroBase/kokoro-v1.0.onnx";
     Size = 325532387; Sha256 = "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5" },
  @{ Name = "voices-v1.0.bin";  Dir = $Kokoro; Url = "$KokoroBase/voices-v1.0.bin";
     Size = 28214398;  Sha256 = "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d" }
)
if (-not $SkipPiper) {
  $Files += @(
    @{ Name = "en_GB-alan-medium.onnx"; Dir = $Voices; Url = "$PiperBase/en_GB-alan-medium.onnx";
       Size = 63201294; Sha256 = "0a309668932205e762801f1efc2736cd4b0120329622adf62be09e56339d3330" },
    @{ Name = "en_GB-alan-medium.onnx.json"; Dir = $Voices; Url = "$PiperBase/en_GB-alan-medium.onnx.json";
       Size = 4888; Sha256 = "c0f0d124e5895c00e7c03b35dcc8287f319a6998a365b182deb5c8e752ee8c1e" }
  )
}

function Get-Sha256([string]$Path) {
  return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Test-Expected([string]$Path, $Spec) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
  $len = (Get-Item -LiteralPath $Path).Length
  if ($Spec.Size -and $len -ne $Spec.Size) { return $false }
  if ($Spec.Sha256 -and (Get-Sha256 $Path) -ne $Spec.Sha256) { return $false }
  return $true
}

$failed = 0
foreach ($f in $Files) {
  New-Item -ItemType Directory -Force -Path $f.Dir | Out-Null
  $dest = Join-Path $f.Dir $f.Name
  if (Test-Expected $dest $f) {
    Write-Host ("ok       {0}  ({1} bytes, sha256 {2})" -f $f.Name, $f.Size, $f.Sha256)
    continue
  }
  $part = "$dest.part"
  if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force -Confirm:$false }
  Write-Host ("fetch    {0}  <- {1}" -f $f.Name, $f.Url)
  try {
    Invoke-WebRequest -Uri $f.Url -OutFile $part -UseBasicParsing
  } catch {
    Write-Warning ("download failed for {0}: {1}" -f $f.Name, $_.Exception.Message)
    if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force -Confirm:$false }
    $failed++
    continue
  }
  $len = (Get-Item -LiteralPath $part).Length
  $hash = Get-Sha256 $part
  if ($f.Size -and $len -ne $f.Size) {
    Write-Warning ("size mismatch for {0}: got {1}, expected {2}" -f $f.Name, $len, $f.Size)
    Remove-Item -LiteralPath $part -Force -Confirm:$false
    $failed++
    continue
  }
  if ($f.Sha256 -and $hash -ne $f.Sha256) {
    Write-Warning ("sha256 mismatch for {0}: got {1}, expected {2}" -f $f.Name, $hash, $f.Sha256)
    Remove-Item -LiteralPath $part -Force -Confirm:$false
    $failed++
    continue
  }
  Move-Item -LiteralPath $part -Destination $dest -Force
  $pin = if ($f.Sha256) { "verified" } else { "NOT PINNED - record this hash" }
  Write-Host ("saved    {0}  ({1} bytes, sha256 {2}, {3})" -f $f.Name, $len, $hash, $pin)
}

if ($failed) {
  Write-Error ("{0} file(s) failed to fetch or verify" -f $failed)
  exit 1
}
Write-Host "all voice files present and verified."
exit 0
