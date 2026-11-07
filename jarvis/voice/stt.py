"""Speech-to-text via faster-whisper (fully local).

The Whisper model is imported and constructed lazily and cached for the life of
the ``Transcriber``. Nothing here ever falls back to a cloud service: if
faster-whisper (or the requested device) is unavailable, transcription reports
``ok=False`` with a clear reason instead of guessing or phoning home.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from ..logging_setup import get_logger

log = get_logger("jarvis.voice.stt")

__all__ = ["Transcriber"]


def _voice(config: Any) -> Any:
    return getattr(config, "voice", config) if config is not None else None


def _numpy() -> Any:
    try:
        import numpy as np
    except Exception:  # pragma: no cover - optional dependency
        return None
    return np


class Transcriber:
    """Wraps a lazily-loaded faster-whisper model."""

    def __init__(self, config: Any = None) -> None:
        v = _voice(config)
        self.model_name = str(getattr(v, "stt_model", "small.en") or "small.en")
        self.device = str(getattr(v, "stt_device", "cpu") or "cpu")
        self.compute_type = str(getattr(v, "stt_compute_type", "int8") or "int8")
        self._model: Any = None
        self._load_lock = threading.Lock()
        self._probe: tuple[bool, str] | None = None

    # -- availability ----------------------------------------------------
    def _probe_impl(self) -> tuple[bool, str]:
        try:
            import faster_whisper  # noqa: F401
        except Exception as exc:  # pragma: no cover - optional dependency
            return False, f"faster-whisper not installed ({exc.__class__.__name__}: {exc})"
        if self.device == "cuda":
            try:
                import ctranslate2
                counter = getattr(ctranslate2, "get_cuda_device_count", None)
                if callable(counter) and counter() < 1:
                    return False, (
                        "stt_device='cuda' but ctranslate2 sees no CUDA device "
                        "(set voice.stt_device='cpu' or install a CUDA runtime)")
            except Exception:  # pragma: no cover - ctranslate2 always ships with fw
                pass
        return True, ""

    async def available(self) -> tuple[bool, str]:
        """``(available, reason)`` — cheap, cached, never imports the model."""
        if self._probe is None:
            self._probe = await asyncio.to_thread(self._probe_impl)
        return self._probe

    # -- model -----------------------------------------------------------
    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel
            log.info("loading faster-whisper model '%s' (device=%s, compute=%s)",
                     self.model_name, self.device, self.compute_type)
            self._model = WhisperModel(self.model_name, device=self.device,
                                       compute_type=self.compute_type)
            return self._model

    # -- transcription ---------------------------------------------------
    @staticmethod
    def _empty(reason: str) -> dict[str, Any]:
        return {"ok": False, "text": "", "language": None, "duration": 0.0,
                "segments": [], "reason": reason}

    @staticmethod
    def _decode_audio(path: str) -> Any:
        """Decode an audio file to 16 kHz mono float32 ourselves.

        We deliberately do NOT hand the path to faster-whisper: its own decoder
        routes through PyAV, and some PyAV builds reject the
        ``av.open(..., metadata_errors=...)`` argument it passes. soundfile
        (libsndfile) handles WAV/FLAC/OGG; the stdlib ``wave`` module is the
        fallback for plain PCM WAV. Returning a numpy array means faster-whisper
        never touches the file at all.
        """
        np = _numpy()
        if np is None:
            return path  # nothing we can do; let faster-whisper try
        sr = 16000
        data: Any = None
        try:
            import soundfile as sf
            data, sr = sf.read(path, dtype="float32", always_2d=False)
        except Exception:
            try:
                import wave as _wave
                with _wave.open(path, "rb") as w:
                    sr = w.getframerate()
                    channels = w.getnchannels()
                    width = w.getsampwidth()
                    raw = w.readframes(w.getnframes())
                if width != 2:
                    return path
                arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                data = arr.reshape(-1, channels)[:, 0] if channels > 1 else arr
            except Exception as exc:
                log.warning("could not decode %s ourselves (%s)", path, exc)
                return path
        a = np.asarray(data, dtype=np.float32)
        if a.ndim > 1:
            a = a[:, 0]
        return Transcriber._resample(a, int(sr or 16000), 16000)

    @staticmethod
    def _resample(arr: Any, sr_from: int, sr_to: int = 16000) -> Any:
        np = _numpy()
        if np is None:
            return arr
        a = np.asarray(arr, dtype=np.float32)
        if a.ndim > 1:
            a = a[:, 0]
        if sr_from == sr_to or len(a) == 0:
            return a
        n_out = max(1, int(round(len(a) * sr_to / sr_from)))
        x_old = np.linspace(0.0, 1.0, num=len(a), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(x_new, x_old, a).astype(np.float32)

    def _transcribe_sync(self, path_or_array: Any, language: str | None,
                         sample_rate: int) -> dict[str, Any]:
        try:
            if isinstance(path_or_array, (str, os.PathLike)):
                audio: Any = self._decode_audio(str(path_or_array))
            else:
                audio = self._resample(path_or_array, sample_rate)
            model = self._load_model()
            segments, info = model.transcribe(audio, language=language, vad_filter=False)
            segs: list[dict[str, Any]] = []
            for seg in segments:
                segs.append({
                    "start": float(getattr(seg, "start", 0.0) or 0.0),
                    "end": float(getattr(seg, "end", 0.0) or 0.0),
                    "text": (getattr(seg, "text", "") or "").strip(),
                })
            text = " ".join(s["text"] for s in segs).strip()
            text = " ".join(text.split())
            return {
                "ok": True,
                "text": text,
                "language": getattr(info, "language", language) or language,
                "duration": float(getattr(info, "duration", 0.0) or 0.0),
                "segments": segs,
                "reason": "",
            }
        except Exception as exc:
            log.warning("transcription failed: %s", exc)
            return self._empty(f"transcription failed: {exc}")

    async def transcribe(self, path_or_array: Any, language: str | None = None, *,
                         sample_rate: int = 16000) -> dict[str, Any]:
        """Transcribe a WAV path or a float32 numpy array.

        Returns ``{"ok", "text", "language", "duration", "segments", "reason"}``.
        Runs in a worker thread so the event loop stays responsive.
        """
        ok, reason = await self.available()
        if not ok:
            return self._empty(reason)
        return await asyncio.to_thread(
            self._transcribe_sync, path_or_array, language, sample_rate)
