"""Decode uploaded audio (browser webm/ogg/opus, wav, flac, mp3...) to 16 kHz mono.

Order of attempts, each reported honestly:

1. ``soundfile`` (libsndfile) straight from memory: WAV, FLAC, OGG/Vorbis and,
   on recent libsndfile, OGG/Opus.
2. ``ffmpeg`` subprocess (``JARVIS_FFMPEG``, PATH, or the WinGet link dir):
   anything ffmpeg understands, including MediaRecorder's WebM/Opus. Input is
   a temp file in a private per-call directory, deleted immediately after.

Returns float32 numpy samples at 16 kHz in ``[-1, 1]``. Never raises for bad
input: the caller gets ``(None, reason)``.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("jarvis.voice.decode")

__all__ = ["TARGET_RATE", "MAX_UPLOAD_BYTES", "MAX_SECONDS", "find_ffmpeg",
           "decode_audio_bytes", "resample", "content_type_allowed"]

TARGET_RATE = 16000
MAX_UPLOAD_BYTES = 25 * 1024 * 1024       # 25 MB is minutes of Opus; plenty
MAX_SECONDS = 120.0                       # longest utterance we will transcribe
FFMPEG_TIMEOUT_S = 30.0

_WINGET_LINKS = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"


def find_ffmpeg() -> str | None:
    override = os.environ.get("JARVIS_FFMPEG", "")
    if override and Path(override).is_file():
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in (_WINGET_LINKS / "ffmpeg.exe", _WINGET_LINKS / "ffmpeg"):
        if cand.is_file():
            return str(cand)
    return None


def resample(np: Any, arr: Any, sr_from: int, sr_to: int = TARGET_RATE) -> Any:
    """Band-limited polyphase resample (scipy), linear-interp fallback."""
    a = np.asarray(arr, dtype=np.float32).reshape(-1)
    if sr_from == sr_to or len(a) == 0:
        return a
    try:
        from math import gcd

        from scipy.signal import resample_poly
        g = gcd(int(sr_from), int(sr_to))
        return resample_poly(a, sr_to // g, sr_from // g).astype(np.float32)
    except Exception:  # pragma: no cover - scipy missing
        n_out = max(1, int(round(len(a) * sr_to / sr_from)))
        x_old = np.linspace(0.0, 1.0, num=len(a), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(x_new, x_old, a).astype(np.float32)


def _via_soundfile(np: Any, data: bytes) -> tuple[Any, str]:
    try:
        import soundfile as sf
    except Exception as exc:
        return None, f"soundfile unavailable ({exc.__class__.__name__})"
    try:
        arr, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception as exc:
        return None, f"soundfile could not decode ({exc})"
    mono = arr.mean(axis=1) if arr.shape[1] > 1 else arr[:, 0]
    return resample(np, mono, int(sr)), ""


def _via_ffmpeg(np: Any, data: bytes) -> tuple[Any, str]:
    """Decode with ffmpeg from a temp file in a private, per-call directory.

    A file (not a pipe) lets ffmpeg seek, which some containers need. The
    directory is created by ``mkdtemp`` (owner-only), ffmpeg gets an argument
    list (no shell), and everything is deleted before returning.
    """
    exe = find_ffmpeg()
    if exe is None:
        return None, "ffmpeg not found (install it or set JARVIS_FFMPEG)"
    workdir = Path(tempfile.mkdtemp(prefix="jarvis-stt-"))
    src = workdir / "input.bin"
    try:
        src.write_bytes(data)
        cmd = [exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(src),
               "-t", f"{MAX_SECONDS:.0f}", "-vn", "-ac", "1", "-ar", str(TARGET_RATE),
               "-f", "f32le", "pipe:1"]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_S,
                                  stdin=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return None, f"ffmpeg timed out after {FFMPEG_TIMEOUT_S:.0f}s"
        except Exception as exc:
            return None, f"ffmpeg failed to run ({exc})"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        return None, f"ffmpeg exited {proc.returncode}: {err or 'no detail'}"
    if len(proc.stdout) < 4:
        return None, "ffmpeg produced no audio"
    usable = len(proc.stdout) - (len(proc.stdout) % 4)
    return np.frombuffer(proc.stdout[:usable], dtype="<f4").astype(np.float32), ""


ALLOWED_CONTENT_TYPES = ("audio/", "video/webm", "video/ogg", "video/mp4",
                         "application/octet-stream")


def content_type_allowed(content_type: str) -> bool:
    """Accept audio/* and the container types browsers label audio with."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return True                      # some clients omit it; decoding decides
    return any(ct == a or (a.endswith("/") and ct.startswith(a)) for a in ALLOWED_CONTENT_TYPES)


def decode_audio_bytes(data: bytes, filename: str = "", content_type: str = "") -> tuple[Any, str]:
    """``(samples_16k_mono_float32, "")`` or ``(None, reason)``."""
    try:
        import numpy as np
    except Exception:
        return None, "numpy is required to decode audio"
    if not data:
        return None, "empty upload"
    if len(data) > MAX_UPLOAD_BYTES:
        return None, f"upload too large ({len(data)} bytes > {MAX_UPLOAD_BYTES})"

    hint = f"{filename} {content_type}".lower()
    looks_compressed_container = any(k in hint for k in ("webm", "mp4", "m4a", "aac", "mpeg", "mp3"))
    reasons: list[str] = []
    attempts = (_via_ffmpeg, _via_soundfile) if looks_compressed_container else (_via_soundfile, _via_ffmpeg)
    for fn in attempts:
        samples, reason = fn(np, data)
        if samples is not None:
            samples = np.clip(np.nan_to_num(samples), -1.0, 1.0)
            limit = int(MAX_SECONDS * TARGET_RATE)
            if len(samples) > limit:
                samples = samples[:limit]
            return samples, ""
        reasons.append(reason)
    return None, "could not decode audio: " + "; ".join(reasons)
