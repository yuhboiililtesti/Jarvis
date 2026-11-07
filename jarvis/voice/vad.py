"""Energy-based voice-activity detection over 16 kHz mono float32 frames.

Pure numpy (imported lazily); a small pure-python fallback keeps the module
importable *and usable* when numpy is absent. There is deliberately no
``webrtcvad`` (or other) third-party VAD dependency: this is a conservative
threshold gate, not a claim of state-of-the-art speech detection.
"""

from __future__ import annotations

import math
from array import array
from typing import Any

from ..logging_setup import get_logger

log = get_logger("jarvis.voice.vad")

__all__ = [
    "DEFAULT_THRESHOLD", "frame_samples", "energy", "is_speech", "VadGate",
    "trim_silence",
]

# RMS energy above which a frame is considered speech. ~ -34 dBFS: comfortably
# above steady room noise, below normal speech. Tunable per call/site.
DEFAULT_THRESHOLD = 0.02


def _numpy() -> Any:
    try:
        import numpy as np
    except Exception:  # pragma: no cover - optional dependency
        return None
    return np


def _as_floats(frame: Any) -> Any:
    """Normalise a frame (numpy array, list, or int16 bytes) to float samples."""
    np = _numpy()
    if np is not None:
        if isinstance(frame, np.ndarray):
            return frame.astype(np.float32, copy=False)
        return np.asarray(frame, dtype=np.float32)
    if isinstance(frame, (bytes, bytearray)):
        return [v / 32768.0 for v in array("h", frame)]
    return [float(v) for v in frame]


def frame_samples(sample_rate: int = 16000, frame_ms: int = 30) -> int:
    """Number of samples in a frame of ``frame_ms`` milliseconds."""
    return max(1, int(sample_rate * frame_ms / 1000))


def energy(frame: Any) -> float:
    """Root-mean-square amplitude of a frame (0.0 for an empty frame)."""
    x = _as_floats(frame)
    n = len(x)
    if n == 0:
        return 0.0
    np = _numpy()
    if np is not None and isinstance(x, np.ndarray):
        return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    total = 0.0
    for v in x:
        fv = float(v)
        total += fv * fv
    return math.sqrt(total / n)


def is_speech(frame: Any, threshold: float = DEFAULT_THRESHOLD,
              *, sample_rate: int | None = None) -> bool:
    """True when the frame's RMS energy exceeds ``threshold``.

    ``sample_rate`` is accepted for API symmetry and future spectral features;
    the energy gate itself is sample-rate independent.
    """
    return energy(frame) >= threshold


class VadGate:
    """Framewise gate detecting begin- and end-of-speech.

    ``feed()`` returns one of ``"start"``, ``"speech"``, ``"end"`` or
    ``"silence"`` so a caller can act on transitions without extra state. End of
    speech requires ``silence_ms`` of continuous low-energy frames; begin of
    speech requires ``min_speech_ms`` of speech so a single click cannot trigger.
    """

    def __init__(self, *, sample_rate: int = 16000, frame_ms: int = 30,
                 silence_ms: int = 700, min_speech_ms: int = 150,
                 threshold: float = DEFAULT_THRESHOLD, pad_ms: int = 100) -> None:
        self.sample_rate = int(sample_rate)
        self.frame_ms = int(frame_ms)
        self.silence_ms = int(silence_ms)
        self.min_speech_ms = int(min_speech_ms)
        self.threshold = float(threshold)
        self.pad_ms = int(pad_ms)
        self.frame_samples = frame_samples(self.sample_rate, self.frame_ms)
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0

    # -- state -----------------------------------------------------------
    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def reset(self) -> None:
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0

    def _start_frames(self) -> int:
        return max(1, math.ceil(self.min_speech_ms / max(1, self.frame_ms)))

    def _silence_frames(self) -> int:
        return max(1, math.ceil(self.silence_ms / max(1, self.frame_ms)))

    # -- framing ---------------------------------------------------------
    def feed(self, frame: Any) -> str:
        """Feed one frame and return the gate state transition."""
        speech = is_speech(frame, self.threshold)
        if speech:
            self._silence_run = 0
            self._speech_run += 1
            if not self._in_speech and self._speech_run >= self._start_frames():
                self._in_speech = True
                return "start"
            return "speech" if self._in_speech else "silence"
        self._speech_run = 0
        if self._in_speech:
            self._silence_run += 1
            if self._silence_run >= self._silence_frames():
                self._in_speech = False
                self._silence_run = 0
                return "end"
            return "speech"
        return "silence"

    def trim_silence(self, signal: Any) -> tuple[Any, bool]:
        """Trim leading/trailing silence from a 1-D signal.

        Returns ``(trimmed_signal, speech_detected)``. When no speech is found
        the returned signal is empty.
        """
        n = len(signal)
        if n == 0:
            return signal[:0], False
        step = self.frame_samples
        first: int | None = None
        last = 0
        for start in range(0, n, step):
            frame = signal[start:start + step]
            if is_speech(frame, self.threshold):
                if first is None:
                    first = start
                last = min(start + step, n)
        if first is None:
            return signal[:0], False
        pad = int(self.sample_rate * self.pad_ms / 1000)
        lo = max(0, first - pad)
        hi = min(n, last + pad)
        return signal[lo:hi], True


def trim_silence(signal: Any, sample_rate: int = 16000, *,
                 frame_ms: int = 30, threshold: float = DEFAULT_THRESHOLD,
                 pad_ms: int = 100) -> tuple[Any, bool]:
    """Convenience wrapper around :meth:`VadGate.trim_silence`."""
    gate = VadGate(sample_rate=sample_rate, frame_ms=frame_ms,
                   threshold=threshold, pad_ms=pad_ms)
    return gate.trim_silence(signal)
