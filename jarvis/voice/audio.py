"""Audio device access, capture and playback.

Everything here degrades gracefully: when ``sounddevice`` (and its PortAudio
backend) is not installed, enumeration returns an empty device list *and a
reason*, capture/playback return a typed failure dict, and nothing raises at
import time. ``sounddevice``, ``soundfile`` and ``numpy`` are imported lazily
inside the helpers so the Core starts with none of the voice stack installed.
"""

from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("jarvis.voice.audio")

__all__ = [
    "DeviceList", "list_input_devices", "list_output_devices", "probe_input",
    "probe_output", "resolve_device", "record", "read_wav", "MicStream", "Player",
]


# ---------------------------------------------------------------------------
# lazy dependency probes
# ---------------------------------------------------------------------------

def _numpy() -> Any:
    """Return the numpy module or ``None`` (never imported at module scope)."""
    try:
        import numpy as np
    except Exception:  # pragma: no cover - optional dependency
        return None
    return np


def _sounddevice() -> tuple[Any, str]:
    """Return ``(sounddevice module, "")`` or ``(None, reason)``.

    Importing ``sounddevice`` touches PortAudio; a present-but-broken backend is
    reported as unavailable rather than allowed to raise later.
    """
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover - optional dependency
        return None, f"sounddevice not installed ({exc.__class__.__name__}: {exc})"
    try:
        sd.query_devices()  # validates that the PortAudio backend is usable
    except Exception as exc:  # pragma: no cover - no audio backend
        return None, f"sounddevice present but audio backend unavailable: {exc}"
    return sd, ""


def _voice(config: Any) -> Any:
    return getattr(config, "voice", config) if config is not None else None


def _rate(config: Any, default: int = 16000) -> int:
    try:
        return int(getattr(_voice(config), "sample_rate", default) or default)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# device enumeration
# ---------------------------------------------------------------------------

class DeviceList(list):
    """A list of device dicts that also carries *why* it may be empty.

    Behaves exactly like a normal ``list`` so ``for d in list_input_devices()``
    works, while ``list_input_devices().reason`` explains an empty result
    ("sounddevice not installed", "no input devices", ...).
    """

    def __init__(self, items: Any = (), reason: str = "") -> None:
        super().__init__(items)
        self.reason = reason


def _collect(sd: Any, want_input: bool) -> DeviceList:
    try:
        devices = sd.query_devices()
    except Exception as exc:  # pragma: no cover - defensive
        return DeviceList([], f"device query failed: {exc}")
    try:
        default = sd.default.device
        default_idx = default[0] if want_input else default[1]
    except Exception:  # pragma: no cover - backend without defaults
        default_idx = -1
    key = "max_input_channels" if want_input else "max_output_channels"
    out: list[dict[str, Any]] = []
    for idx, dev in enumerate(devices):
        try:
            channels = int(dev.get(key, 0))
        except (AttributeError, TypeError, ValueError):
            channels = 0
        if channels <= 0:
            continue
        out.append({
            "index": idx,
            "name": str(dev.get("name", f"device {idx}")),
            "channels": channels,
            "default": idx == default_idx,
        })
    return DeviceList(out, "")


def list_input_devices() -> DeviceList:
    """Input (microphone) devices, or an empty list carrying a reason."""
    sd, reason = _sounddevice()
    if sd is None:
        return DeviceList([], reason)
    return _collect(sd, want_input=True)


def list_output_devices() -> DeviceList:
    """Output (speaker) devices, or an empty list carrying a reason."""
    sd, reason = _sounddevice()
    if sd is None:
        return DeviceList([], reason)
    return _collect(sd, want_input=False)


def resolve_device(sd: Any, spec: str | None, want_input: bool) -> tuple[int | None, str]:
    """Resolve a device name-substring (or numeric index) to a device index.

    Empty ``spec`` selects the system default (``None``). A non-empty spec that
    matches nothing returns ``(None, reason)`` so the caller can report it.
    """
    spec = (spec or "").strip()
    if not spec:
        return None, ""
    if spec.isdigit():
        return int(spec), ""
    devices = _collect(sd, want_input)
    if not devices:
        return None, devices.reason or "no matching audio device"
    low = spec.lower()
    for dev in devices:
        if low in dev["name"].lower():
            return dev["index"], ""
    kind = "input" if want_input else "output"
    return None, f"no {kind} device name contains '{spec}'"


def probe_input(config: Any = None) -> tuple[bool, str]:
    """``(available, reason)`` for microphone capture."""
    sd, reason = _sounddevice()
    if sd is None:
        return False, reason
    if _numpy() is None:
        return False, "numpy is required for audio capture"
    spec = getattr(_voice(config), "input_device", "") or ""
    idx, dreason = resolve_device(sd, spec, want_input=True)
    if spec and idx is None:
        return False, dreason
    devices = _collect(sd, want_input=True)
    if not devices:
        return False, "no input (microphone) devices found"
    return True, ""


def probe_output(config: Any = None) -> tuple[bool, str]:
    """``(available, reason)`` for speaker playback."""
    sd, reason = _sounddevice()
    if sd is None:
        return False, reason
    if _numpy() is None:
        return False, "numpy is required for audio playback"
    spec = getattr(_voice(config), "output_device", "") or ""
    idx, dreason = resolve_device(sd, spec, want_input=False)
    if spec and idx is None:
        return False, dreason
    devices = _collect(sd, want_input=False)
    if not devices:
        return False, "no output (speaker) devices found"
    return True, ""


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------

def record(seconds: float, *, samplerate: int | None = None, device: str | None = None,
           config: Any = None, channels: int = 1) -> dict[str, Any]:
    """Blocking capture of ``seconds`` of mono float32 audio.

    Returns ``{"ok", "data", "samplerate", "reason"}``; ``data`` is a 1-D numpy
    float32 array. Call from a worker thread (``asyncio.to_thread``).
    """
    sd, reason = _sounddevice()
    if sd is None:
        return {"ok": False, "data": None, "samplerate": None, "reason": reason}
    np = _numpy()
    if np is None:
        return {"ok": False, "data": None, "samplerate": None,
                "reason": "numpy is required for audio capture"}
    sr = int(samplerate or _rate(config))
    spec = device if device is not None else (getattr(_voice(config), "input_device", "") or "")
    dev, dreason = resolve_device(sd, spec, want_input=True)
    if spec and dev is None:
        return {"ok": False, "data": None, "samplerate": sr, "reason": dreason}
    frames = max(1, int(sr * max(0.0, seconds)))
    try:
        data = sd.rec(frames, samplerate=sr, channels=channels, dtype="float32", device=dev)
        sd.wait()
    except Exception as exc:
        return {"ok": False, "data": None, "samplerate": sr,
                "reason": f"capture failed: {exc}"}
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr[:, 0]
    return {"ok": True, "data": arr.reshape(-1), "samplerate": sr, "reason": ""}


class MicStream:
    """A callback-driven microphone stream feeding fixed-size frames.

    Used by the wake-word loop. Frames are 1-D float32 numpy arrays. The stream
    drops the oldest frame on overflow so a slow consumer can never stall the
    audio callback.
    """

    def __init__(self, config: Any = None, *, device: str | None = None,
                 samplerate: int | None = None, frame_ms: int = 30,
                 max_frames: int = 64) -> None:
        v = _voice(config)
        self.samplerate = int(samplerate or _rate(config))
        self.device_spec = device if device is not None else (
            getattr(v, "input_device", "") or "")
        self.frame_ms = frame_ms
        self.frame_samples = max(1, int(self.samplerate * frame_ms / 1000))
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_frames)
        self._stream: Any = None

    def available(self) -> tuple[bool, str]:
        sd, reason = _sounddevice()
        if sd is None:
            return False, reason
        if _numpy() is None:
            return False, "numpy is required for audio capture"
        dev, dreason = resolve_device(sd, self.device_spec, want_input=True)
        if self.device_spec and dev is None:
            return False, dreason
        return True, ""

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        try:
            frame = indata[:, 0].copy()
        except Exception:  # pragma: no cover - defensive
            return
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - raced
                pass
            try:
                self._queue.put_nowait(frame)
            except queue.Full:  # pragma: no cover - raced
                pass

    def start(self) -> tuple[bool, str]:
        ok, reason = self.available()
        if not ok:
            return False, reason
        sd, _ = _sounddevice()
        dev, _ = resolve_device(sd, self.device_spec, want_input=True)
        try:
            self._stream = sd.InputStream(
                samplerate=self.samplerate, channels=1, dtype="float32",
                blocksize=self.frame_samples, device=dev, callback=self._callback)
            self._stream.start()
        except Exception as exc:
            self._stream = None
            return False, f"could not open input stream: {exc}"
        log.info("microphone stream open (%d Hz, frame=%d samples)",
                 self.samplerate, self.frame_samples)
        return True, ""

    def get_frame(self, timeout: float = 1.0) -> Any:
        """Blocking read of the next frame, or ``None`` on timeout."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("input stream close failed: %s", exc)
            finally:
                self._stream = None


# ---------------------------------------------------------------------------
# playback
# ---------------------------------------------------------------------------

def read_wav(path: str | Path) -> tuple[Any, int]:
    """Read a WAV file as ``(float32 mono array or list, samplerate)``.

    Prefers ``soundfile``; falls back to the stdlib ``wave`` module (16-bit
    PCM). Raises on failure so callers can report a clear reason.
    """
    try:
        import soundfile as sf
    except Exception:
        sf = None
    if sf is not None:
        data, sr = sf.read(str(path), dtype="float32", always_2d=False)
        return data, int(sr)
    np = _numpy()
    if np is None:
        raise RuntimeError("numpy or soundfile is required to read an audio file")
    import wave as _wave
    with _wave.open(str(path), "rb") as w:
        nch = w.getnchannels()
        width = w.getsampwidth()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise RuntimeError(f"unsupported WAV sample width {width} bytes (need 16-bit PCM)")
    arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if nch > 1:
        arr = arr.reshape(-1, nch).mean(axis=1)
    return arr, int(sr)


class Player:
    """Plays float32 audio arrays or WAV files through sounddevice."""

    def __init__(self, config: Any = None, *, device: str | None = None,
                 samplerate: int | None = None, volume: float | None = None) -> None:
        v = _voice(config)
        self.samplerate = int(samplerate or _rate(config))
        self.device_spec = device if device is not None else (
            getattr(v, "output_device", "") or "")
        default_vol = getattr(v, "volume", 0.65) if v is not None else 0.65
        try:
            self.volume = max(0.0, min(1.0, float(
                volume if volume is not None else default_vol)))
        except (TypeError, ValueError):
            self.volume = 0.65

    def available(self) -> tuple[bool, str]:
        sd, reason = _sounddevice()
        if sd is None:
            return False, reason
        if _numpy() is None:
            return False, "numpy is required for audio playback"
        dev, dreason = resolve_device(sd, self.device_spec, want_input=False)
        if self.device_spec and dev is None:
            return False, dreason
        return True, ""

    def play_array(self, data: Any, samplerate: int | None = None) -> dict[str, Any]:
        """Blocking playback of a float32 array. Call from a worker thread."""
        sd, reason = _sounddevice()
        if sd is None:
            return {"ok": False, "reason": reason}
        np = _numpy()
        if np is None:
            return {"ok": False, "reason": "numpy is required for audio playback"}
        arr = np.asarray(data, dtype=np.float32)
        if arr.ndim > 1:
            arr = arr[:, 0]
        if self.volume != 1.0:
            # Volume is applied here so the setting also affects voices that
            # were synthesised at full scale (e.g. an imported WAV).
            arr = np.clip(arr * self.volume, -1.0, 1.0)
        sr = int(samplerate or self.samplerate)
        dev, dreason = resolve_device(sd, self.device_spec, want_input=False)
        if self.device_spec and dev is None:
            return {"ok": False, "reason": dreason}
        try:
            sd.play(arr.reshape(-1), sr, device=dev)
            sd.wait()
        except Exception as exc:
            return {"ok": False, "reason": f"playback failed: {exc}"}
        seconds = float(len(arr)) / sr if sr else 0.0
        return {"ok": True, "reason": "", "samplerate": sr, "seconds": seconds}

    def play_file(self, path: str | Path) -> dict[str, Any]:
        """Blocking playback of a WAV file. Call from a worker thread."""
        try:
            data, sr = read_wav(path)
        except Exception as exc:
            return {"ok": False, "reason": f"could not read audio file: {exc}"}
        return self.play_array(data, samplerate=sr)
