#!/usr/bin/env bash
# Fetch the local TTS voice models JARVIS uses, with size + SHA-256 checks.
#
# Downloads (only when missing or corrupt):
#   * Kokoro v1.0 ONNX model + voice pack  -> data/voices/kokoro/
#   * Piper en_GB-alan-medium (fallback)   -> data/voices/
#
# Every file is downloaded to "<name>.part", checked against the expected byte
# size and SHA-256, and only then renamed into place. Re-running is idempotent.
#
# Hash provenance (same pins as scripts/fetch-voices.ps1):
#   * Piper alan: SHA-256 equals the Hugging Face LFS ETag.
#   * Kokoro: pinned from the first verified download (trust-on-first-use).
#
# Usage:  bash scripts/fetch-voices.sh [--skip-piper]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOICES="$ROOT/data/voices"
KOKORO="$VOICES/kokoro"
SKIP_PIPER=0
[ $# -gt 0 ] && [ "$1" = "--skip-piper" ] && SKIP_PIPER=1

KOKORO_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium"

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

expected() {
  # expected <path> <size> <sha256>  -> 0 if present and correct
  [ -f "$1" ] || return 1
  local len hash
  len=$(wc -c < "$1")
  hash=$(sha256_of "$1")
  [ "$len" = "$2" ] && [ "$hash" = "$3" ]
}

fetch_one() {
  # fetch_one <dir> <name> <url> <size> <sha256>
  local dir="$1" name="$2" url="$3" size="$4" hash="$5"
  mkdir -p "$dir"
  local dest="$dir/$name" part="$dest.part"
  if expected "$dest" "$size" "$hash"; then
    echo "ok       $name  ($size bytes, sha256 $hash)"
    return 0
  fi
  rm -f "$part"
  echo "fetch    $name  <- $url"
  if ! curl -fsSL --retry 2 -o "$part" "$url"; then
    echo "WARNING: download failed for $name" >&2
    rm -f "$part"
    return 1
  fi
  local len hash2
  len=$(wc -c < "$part")
  hash2=$(sha256_of "$part")
  if [ "$len" != "$size" ]; then
    echo "WARNING: size mismatch for $name: got $len, expected $size" >&2
    rm -f "$part"
    return 1
  fi
  if [ "$hash2" != "$hash" ]; then
    echo "WARNING: sha256 mismatch for $name: got $hash2, expected $hash" >&2
    rm -f "$part"
    return 1
  fi
  mv "$part" "$dest"
  echo "saved    $name  ($len bytes, sha256 $hash2, verified)"
}

failed=0
fetch_one "$KOKORO" "kokoro-v1.0.onnx" "$KOKORO_BASE/kokoro-v1.0.onnx" \
  325532387 "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5" || failed=$((failed+1))
fetch_one "$KOKORO" "voices-v1.0.bin" "$KOKORO_BASE/voices-v1.0.bin" \
  28214398 "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d" || failed=$((failed+1))

if [ "$SKIP_PIPER" = 0 ]; then
  fetch_one "$VOICES" "en_GB-alan-medium.onnx" "$PIPER_BASE/en_GB-alan-medium.onnx" \
    63201294 "0a309668932205e762801f1efc2736cd4b0120329622adf62be09e56339d3330" || failed=$((failed+1))
  fetch_one "$VOICES" "en_GB-alan-medium.onnx.json" "$PIPER_BASE/en_GB-alan-medium.onnx.json" \
    4888 "c0f0d124e5895c00e7c03b35dcc8287f319a6998a365b182deb5c8e752ee8c1e" || failed=$((failed+1))
fi

if [ "$failed" -gt 0 ]; then
  echo "$failed file(s) failed to fetch or verify" >&2
  exit 1
fi
echo "all voice files present and verified."
