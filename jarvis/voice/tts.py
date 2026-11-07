"""Text-to-speech: an honest engine chain over local voices.

Engines (``config.voice.tts_engine`` + ``config.voice.tts_fallback``):

* ``kokoro`` — Kokoro-82M v1.0 via ``kokoro-onnx`` on onnxruntime **CPU**. Model
  files live in ``config.kokoro_path`` (``kokoro-v1.0.onnx`` + ``voices-v1.0.bin``,
  fetched by ``scripts/fetch-voices.ps1``). The model is loaded once per process
  and shared by every ``Speaker`` (thread-safe; synthesis is serialised per model).
* ``piper`` — a local Piper voice (``.onnx`` + ``.onnx.json`` in
  ``config.voices_path``) through the ``piper`` CLI or python package.
* ``null`` — no synthesiser. Writes a silent placeholder WAV and reports
  ``ok=False, speech=False``. Always last in the chain.

The chain is ``[tts_engine] + tts_fallback + ["null"]``. Every result reports the
engine actually used, each attempt, and why earlier engines were skipped.
Real speech is passed through :mod:`jarvis.voice.effects` (``voice.effect``,
``voice.effect_strength``) and written as 16-bit PCM WAV.

The ``Speaker`` only produces audio files; playback is the caller's job (see
``jarvis.voice.audio.Player`` or the HUD). No cloud TTS is ever substituted, and
there is no voice-cloning engine.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, Protocol

from ..logging_setup import get_logger
from .effects import apply_effect, normalise_effect
from .text import clean_for_speech

log = get_logger("jarvis.voice.tts")

__all__ = ["Speaker", "DEFAULT_VOICE", "DEFAULT_KOKORO_VOICE", "ENGINES",
           "KOKORO_BRITISH_VOICES", "KokoroEngine", "PiperEngine", "write_wav"]

DEFAULT_VOICE = "en_GB-alan-medium"
DEFAULT_KOKORO_VOICE = "bm_george"
ENGINES = ("kokoro", "piper", "null")
KOKORO_MODEL_FILE = "kokoro-v1.0.onnx"
KOKORO_VOICES_FILE = "voices-v1.0.bin"

# Stock British voices shipped in voices-v1.0.bin (male first: the butler).
KOKORO_BRITISH_VOICES: tuple[dict[str, str], ...] = (
    {"id": "bm_george", "label": "George", "accent": "British", "gender": "male"},
    {"id": "bm_lewis", "label": "Lewis", "accent": "British", "gender": "male"},
    {"id": "bm_daniel", "label": "Daniel", "accent": "British", "gender": "male"},
    {"id": "bm_fable", "label": "Fable", "accent": "British", "gender": "male"},
)
_KOKORO_VOICE_RE = re.compile(r"^[a-z]{2}_[a-z]+$")


def _voice(config: Any) -> Any:
    return getattr(config, "voice", config) if config is not None else None


def _voices_dir(config: Any) -> Path:
    """Resolve the Piper voice directory, matching ``Config.voices_path``."""
    if config is not None:
        try:
            path = getattr(config, "voices_path", None)
            if path:
                return Path(path)
        except Exception:  # pragma: no cover - defensive
            pass
    v = _voice(config)
    configured = getattr(v, "piper_dir", "") if v is not None else ""
    if configured:
        return Path(configured)
    try:
        from ..config import DATA_DIR
        return Path(DATA_DIR) / "voices"
    except Exception:  # pragma: no cover - defensive
        return Path("data") / "voices"


def _kokoro_dir(config: Any) -> Path:
    if config is not None:
        try:
            path = getattr(config, "kokoro_path", None)
            if path:
                return Path(path)
        except Exception:  # pragma: no cover - defensive
            pass
    v = _voice(config)
    configured = getattr(v, "kokoro_dir", "") if v is not None else ""
    if configured:
        return Path(configured)
    return _voices_dir(config) / "kokoro"


TEMP_TTL_S = 15 * 60          # synthesized WAVs are temporary (privacy rule)
_last_sweep = 0.0


def temp_dir() -> Path:
    base = Path(tempfile.gettempdir()) / "jarvis-voice"
    base.mkdir(parents=True, exist_ok=True)
    return base


def sweep_temp(max_age_s: float = TEMP_TTL_S, *, force: bool = False) -> int:
    """Delete ``tts-*.wav`` older than ``max_age_s`` from the voice temp dir.

    Runs at most once a minute unless ``force``. Returns files removed.
    """
    import time as _time
    global _last_sweep
    now = _time.time()
    if not force and now - _last_sweep < 60.0:
        return 0
    _last_sweep = now
    removed = 0
    for p in temp_dir().glob("tts-*.wav"):
        try:
            if now - p.stat().st_mtime > max_age_s:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _temp_wav() -> Path:
    sweep_temp()
    fd, name = tempfile.mkstemp(prefix="tts-", suffix=".wav", dir=str(temp_dir()))
    os.close(fd)
    return Path(name)


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
        return float(frames) / float(rate) if rate else 0.0
    except Exception:  # pragma: no cover - corrupt/absent file
        return 0.0


def _write_silence(path: Path, seconds: float, sample_rate: int = 16000) -> float:
    seconds = max(0.0, float(seconds))
    frames = int(sample_rate * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * frames)
    return frames / sample_rate if sample_rate else 0.0


def write_wav(path: str | Path, samples: Any, sample_rate: int) -> float:
    """Write mono float samples in [-1, 1] as 16-bit PCM; returns seconds."""
    import numpy as np
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).round().astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return len(arr) / float(sample_rate) if sample_rate else 0.0


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def _clamp01(value: Any, default: float) -> float:
    return _clamp(value, 0.0, 1.0, default)


def _synthesis_config(length_scale: float, volume: float) -> Any:
    """Build a piper SynthesisConfig when this release exposes one, else None."""
    try:
        from piper import SynthesisConfig  # type: ignore
    except Exception:
        return None
    try:
        return SynthesisConfig(length_scale=length_scale, volume=volume)
    except TypeError:
        try:
            return SynthesisConfig(length_scale=length_scale)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# engine protocol
# ---------------------------------------------------------------------------

class Engine(Protocol):
    name: str

    def probe(self) -> tuple[bool, str]: ...

    def synth(self, text: str, voice: str | None = None) -> tuple[Any, int, dict[str, Any]]:
        """Return ``(float32 mono samples, sample_rate, detail)``; raise on failure."""
        ...


# ---------------------------------------------------------------------------
# kokoro
# ---------------------------------------------------------------------------

_KOKORO_MODELS: dict[tuple[str, str], tuple[Any, threading.Lock]] = {}
_KOKORO_GUARD = threading.Lock()


def _kokoro_threads() -> int:
    """CPU threads for Kokoro. Modest by default so a game keeps its cores."""
    raw = os.environ.get("JARVIS_KOKORO_THREADS", "").strip()
    try:
        return max(1, int(raw)) if raw else 4
    except ValueError:
        return 4


def _load_kokoro(model: Path, voices: Path) -> tuple[Any, threading.Lock]:
    key = (str(model.resolve()), str(voices.resolve()))
    with _KOKORO_GUARD:
        hit = _KOKORO_MODELS.get(key)
        if hit is not None:
            return hit
        import onnxruntime as rt
        from kokoro_onnx import Kokoro
        opts = rt.SessionOptions()
        opts.intra_op_num_threads = _kokoro_threads()
        opts.inter_op_num_threads = 1
        # CPU only, explicitly: never pick up a GPU provider by accident.
        session = rt.InferenceSession(str(model), sess_options=opts,
                                      providers=["CPUExecutionProvider"])
        kokoro = Kokoro.from_session(session, str(voices))
        log.info("kokoro model loaded (%s, providers=%s, threads=%d)",
                 model.name, session.get_providers(), opts.intra_op_num_threads)
        hit = (kokoro, threading.Lock())
        _KOKORO_MODELS[key] = hit
        return hit


class KokoroEngine:
    name = "kokoro"

    def __init__(self, speaker: "Speaker") -> None:
        self.speaker = speaker

    @property
    def model_path(self) -> Path:
        return self.speaker.kokoro_dir / KOKORO_MODEL_FILE

    @property
    def voices_path(self) -> Path:
        return self.speaker.kokoro_dir / KOKORO_VOICES_FILE

    def installed(self) -> bool:
        return self.model_path.is_file() and self.voices_path.is_file()

    def probe(self) -> tuple[bool, str]:
        for mod in ("kokoro_onnx", "onnxruntime"):
            if importlib.util.find_spec(mod) is None:
                return False, f"{mod} not installed (pip install kokoro-onnx)"
        if not self.model_path.is_file():
            return False, f"kokoro model missing: {self.model_path} (run scripts/fetch-voices.ps1)"
        if not self.voices_path.is_file():
            return False, f"kokoro voices missing: {self.voices_path} (run scripts/fetch-voices.ps1)"
        if not _KOKORO_VOICE_RE.match(self.speaker.kokoro_voice):
            return False, f"invalid kokoro voice id '{self.speaker.kokoro_voice}'"
        return True, ""

    def warm(self) -> None:
        _load_kokoro(self.model_path, self.voices_path)

    def synth(self, text: str, voice: str | None = None) -> tuple[Any, int, dict[str, Any]]:
        import numpy as np
        name = voice or self.speaker.kokoro_voice
        if not _KOKORO_VOICE_RE.match(name):
            raise ValueError(f"invalid kokoro voice id '{name}'")
        kokoro, lock = _load_kokoro(self.model_path, self.voices_path)
        lang = "en-gb" if name[:1] == "b" else "en-us"
        with lock:
            if name not in kokoro.get_voices():
                raise ValueError(f"kokoro voice '{name}' not in {self.voices_path.name}")
            samples, sr = kokoro.create(text, voice=name, speed=self.speaker.kokoro_speed,
                                        lang=lang)
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            raise RuntimeError("kokoro produced no audio")
        return arr, int(sr), {"voice": name, "lang": lang, "model": str(self.model_path)}


# ---------------------------------------------------------------------------
# piper
# ---------------------------------------------------------------------------

class PiperEngine:
    name = "piper"

    def __init__(self, speaker: "Speaker") -> None:
        self.speaker = speaker

    def backend(self) -> tuple[str | None, str | None]:
        """Return ``(kind, path)`` where kind is ``"binary"`` or ``"python"``."""
        sp = self.speaker
        if sp.bin_override:
            candidate = Path(sp.bin_override)
            if candidate.is_file():
                return "binary", str(candidate)
            found = shutil.which(sp.bin_override)
            if found:
                return "binary", found
        found = shutil.which("piper")
        if found:
            return "binary", found
        # A venv install puts the console script beside the interpreter, and that
        # directory is frequently NOT on PATH. Prefer this stable CLI over the
        # python API, whose signature drifts between piper-tts releases.
        scripts = Path(sys.executable).parent
        for cand in (scripts / "piper.exe", scripts / "piper",
                     scripts / "piper.cmd", scripts / "piper.bat"):
            if cand.is_file():
                return "binary", str(cand)
        if importlib.util.find_spec("piper") is None:
            return None, None
        return "python", None

    def find_model(self, name: str | None = None) -> tuple[Path | None, str]:
        """Locate the ``.onnx`` voice model for ``name`` (default: configured)."""
        sp = self.speaker
        name = name or sp.piper_voice
        direct = Path(name)
        if direct.suffix == ".onnx" and direct.is_file():
            return direct, ""
        if not sp.voices_dir.is_dir():
            return None, f"voice directory not found: {sp.voices_dir}"
        exact = sp.voices_dir / f"{name}.onnx"
        if exact.is_file():
            return exact, ""
        hits = self.installed_models()
        for hit in hits:
            if hit.stem == name or hit.name == name:
                return hit, ""
        if hits:
            return hits[0], ""
        return None, f"no piper voice model matching '{name}' in {sp.voices_dir}"

    def installed_models(self) -> list[Path]:
        """Piper voices = ``*.onnx`` with a sibling ``.onnx.json`` (kokoro/ excluded)."""
        d = self.speaker.voices_dir
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.rglob("*.onnx")):
            if p.with_suffix(".onnx.json").is_file():
                out.append(p)
        return out

    def probe(self) -> tuple[bool, str]:
        kind, _path = self.backend()
        if kind is None:
            return False, ("piper not found: install piper-tts, put 'piper' on PATH, "
                           "or set JARVIS_PIPER_BIN")
        model, reason = self.find_model()
        if model is None:
            return False, reason
        if not model.with_suffix(".onnx.json").is_file():
            return False, f"missing voice config '{model.name}.json' beside {model}"
        return True, ""

    def synth(self, text: str, voice: str | None = None) -> tuple[Any, int, dict[str, Any]]:
        from .audio import read_wav
        kind, binpath = self.backend()
        model, reason = self.find_model(voice)
        if kind is None or model is None:
            raise RuntimeError(reason or "piper backend unavailable")
        sp = self.speaker
        tmp = _temp_wav()
        try:
            if kind == "binary":
                proc = subprocess.run(
                    [str(binpath), "--model", str(model), "--output_file", str(tmp),
                     "--length-scale", f"{sp.length_scale:.3f}",
                     # Full scale here: playback (server Player / HUD) applies the
                     # owner's volume exactly once.
                     "--volume", "1.000",
                     "--sentence-silence", f"{sp.sentence_silence:.3f}"],
                    input=text.encode("utf-8"), capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode != 0:
                    err = (proc.stderr or b"").decode("utf-8", "replace")[:300]
                    raise RuntimeError(f"piper exited {proc.returncode}: {err}")
            else:
                self._synth_python(text, model, tmp)
            data, sr = read_wav(tmp)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        import numpy as np
        arr = np.asarray(data, dtype=np.float32)
        if arr.ndim > 1:
            arr = arr.mean(axis=1)
        if arr.size == 0:
            raise RuntimeError("piper produced no audio")
        return arr, int(sr), {"voice": model.stem, "model": str(model), "backend": kind}

    def _synth_python(self, text: str, model: Path, target: Path) -> None:
        """Synthesise with the ``piper`` python package (new or legacy API).

        piper-tts changed the contract here: older releases configure the wave
        handle themselves, newer ones write frames into a handle YOU must
        configure first (otherwise ``wave`` raises "# channels not specified").
        Configure up-front, then fall back to the chunk-yielding API.
        """
        from piper import PiperVoice
        voice = PiperVoice.load(str(model))
        cfg = getattr(voice, "config", None)
        sample_rate = int(getattr(cfg, "sample_rate", 0) or 22050)
        channels = int(getattr(cfg, "num_channels", 1) or 1)
        syn_config = _synthesis_config(self.speaker.length_scale, 1.0)
        try:
            with wave.open(str(target), "wb") as wav_file:
                wav_file.setnchannels(channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                if syn_config is not None:
                    voice.synthesize(text, wav_file, syn_config)
                else:
                    voice.synthesize(text, wav_file)
            return
        except (TypeError, wave.Error):
            pass
        with wave.open(str(target), "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            iterator = (voice.synthesize(text, syn_config) if syn_config is not None
                        else voice.synthesize(text))
            for chunk in iterator:
                data = getattr(chunk, "audio_int16_bytes", None)
                if data is None:
                    data = bytes(chunk)
                wav_file.writeframes(data)


# ---------------------------------------------------------------------------
# speaker (the chain)
# ---------------------------------------------------------------------------

def _norm_engine(name: Any, default: str = "kokoro") -> str:
    n = str(name or default).strip().lower()
    return n if n in ENGINES else default


class Speaker:
    """Synthesise speech to a WAV file through the configured engine chain."""

    def __init__(self, config: Any = None, *, engines: dict[str, Any] | None = None) -> None:
        v = _voice(config)
        self.engine = _norm_engine(getattr(v, "tts_engine", "kokoro"), "kokoro")
        raw_fb = getattr(v, "tts_fallback", ["piper"])
        if isinstance(raw_fb, str):
            raw_fb = [raw_fb]
        self.fallback = [e for e in (_norm_engine(x, "") for x in (raw_fb or [])) if e]
        self.piper_voice = str(getattr(v, "piper_voice", DEFAULT_VOICE) or DEFAULT_VOICE)
        self.kokoro_voice = str(getattr(v, "kokoro_voice", DEFAULT_KOKORO_VOICE)
                                or DEFAULT_KOKORO_VOICE)
        self.kokoro_speed = _clamp(getattr(v, "kokoro_speed", 1.0), 0.5, 2.0, 1.0)
        self.effect = normalise_effect(getattr(v, "effect", "jarvis"))
        self.effect_strength = _clamp01(getattr(v, "effect_strength", 0.5), 0.5)
        self.sample_rate = int(getattr(v, "sample_rate", 16000) or 16000)
        self.voices_dir = _voices_dir(config)
        self.kokoro_dir = _kokoro_dir(config)
        override = getattr(v, "piper_bin", "") if v is not None else ""
        self.bin_override = str(override or os.environ.get("JARVIS_PIPER_BIN", "") or "")
        # Delivery controls (owner-tunable).
        self.volume = _clamp01(getattr(v, "volume", 0.65), 0.65)
        self.length_scale = _clamp(getattr(v, "length_scale", 0.9), 0.5, 2.0, 0.9)
        self.sentence_silence = _clamp(
            getattr(v, "sentence_silence", 0.08), 0.0, 1.0, 0.08)
        self.engines: dict[str, Any] = engines if engines is not None else {
            "kokoro": KokoroEngine(self), "piper": PiperEngine(self)}
        self._probes: dict[str, tuple[bool, str]] = {}
        self._probe_lock = threading.Lock()

    # -- configuration ----------------------------------------------------
    @property
    def chain(self) -> list[str]:
        out: list[str] = []
        for name in [self.engine, *self.fallback, "null"]:
            if name not in out:
                out.append(name)
        if "null" in out:  # null is always the honest last resort
            out.remove("null")
            out.append("null")
        return out

    @property
    def voice_name(self) -> str:
        if self.engine == "kokoro":
            return self.kokoro_voice
        if self.engine == "piper":
            return self.piper_voice
        return "none"

    def configure(self, *, engine: Any = None, voice: Any = None, effect: Any = None,
                  strength: Any = None, speed: Any = None) -> dict[str, Any]:
        """Change engine/voice/effect at runtime. Raises ``ValueError`` on bad input."""
        applied: dict[str, Any] = {}
        if engine is not None:
            name = str(engine).strip().lower()
            if name not in ENGINES:
                raise ValueError(f"unknown engine '{engine}' (expected one of {ENGINES})")
            self.engine = name
            applied["engine"] = name
        if voice is not None:
            vid = str(voice).strip()
            if self.engine == "kokoro":
                if not _KOKORO_VOICE_RE.match(vid):
                    raise ValueError(f"invalid kokoro voice id '{vid}'")
                self.kokoro_voice = vid
            elif self.engine == "piper":
                names = {p.stem for p in PiperEngine(self).installed_models()}
                if vid not in names:
                    raise ValueError(f"piper voice '{vid}' is not installed in {self.voices_dir}")
                self.piper_voice = vid
            else:
                raise ValueError("the null engine has no voices")
            applied["voice"] = vid
        if effect is not None:
            name = str(effect).strip().lower()
            if name not in ("jarvis", "none"):
                raise ValueError("effect must be 'jarvis' or 'none'")
            self.effect = name
            applied["effect"] = name
        if strength is not None:
            self.effect_strength = _clamp01(strength, self.effect_strength)
            applied["effect_strength"] = self.effect_strength
        if speed is not None:
            self.kokoro_speed = _clamp(speed, 0.5, 2.0, self.kokoro_speed)
            applied["speed"] = self.kokoro_speed
        with self._probe_lock:
            self._probes.clear()
        return applied

    # -- availability -----------------------------------------------------
    def probe_engine(self, name: str) -> tuple[bool, str]:
        if name == "null":
            return False, "null engine: silent placeholder only"
        with self._probe_lock:
            hit = self._probes.get(name)
        if hit is not None:
            return hit
        eng = self.engines.get(name)
        if eng is None:
            res = (False, f"engine '{name}' not built")
        else:
            try:
                res = eng.probe()
            except Exception as exc:  # pragma: no cover - defensive
                res = (False, f"{name} probe failed: {exc}")
        with self._probe_lock:
            self._probes[name] = res
        return res

    def _probe_impl(self) -> tuple[bool, str]:
        if self.engine == "null":
            return False, "tts_engine='null': no speech synthesiser configured"
        skipped: list[str] = []
        for name in self.chain:
            if name == "null":
                break
            ok, reason = self.probe_engine(name)
            if ok:
                if skipped:
                    return True, f"using {name}; " + "; ".join(skipped)
                return True, ""
            skipped.append(f"{name} unavailable: {reason}")
        return False, "; ".join(skipped) or "no speech engine available"

    async def available(self) -> tuple[bool, str]:
        """``(available, reason)`` — cached per engine; never downloads or synthesises."""
        return await asyncio.to_thread(self._probe_impl)

    def active_engine(self) -> str:
        """First engine in the chain that probes available (``null`` if none)."""
        for name in self.chain:
            if name == "null" or self.probe_engine(name)[0]:
                return name
        return "null"

    def backend_name(self) -> str:
        if self.engine == "null":
            return "null"
        if self.engine == "kokoro":
            return "kokoro-onnx/cpu"
        kind, _ = PiperEngine(self).backend()
        return kind or "none"

    def warm(self) -> tuple[bool, str]:
        """Load the active model now (e.g. at startup) so the first reply is fast."""
        name = self.active_engine()
        eng = self.engines.get(name)
        warm = getattr(eng, "warm", None)
        if warm is None:
            return name != "null", f"{name}: nothing to warm"
        try:
            warm()
            return True, f"{name} loaded"
        except Exception as exc:
            return False, f"{name} warm-up failed: {exc}"

    def list_voices(self) -> dict[str, Any]:
        kok = self.engines.get("kokoro")
        pip = self.engines.get("piper")
        k_ok, k_reason = self.probe_engine("kokoro")
        p_ok, p_reason = self.probe_engine("piper")
        piper_voices = []
        if isinstance(pip, PiperEngine):
            for p in pip.installed_models():
                piper_voices.append({"id": p.stem, "label": p.stem,
                                     "accent": "British" if "en_GB" in p.stem else "",
                                     "gender": ""})
        return {
            "kokoro": {"available": k_ok, "reason": k_reason,
                       "installed": bool(getattr(kok, "installed", lambda: False)()),
                       "voices": [dict(v) for v in KOKORO_BRITISH_VOICES]},
            "piper": {"available": p_ok, "reason": p_reason, "voices": piper_voices},
            "null": {"available": True, "reason": "silent placeholder; not speech",
                     "voices": []},
        }

    # -- synthesis --------------------------------------------------------
    @staticmethod
    def _estimate_seconds(text: str) -> float:
        words = max(1, len(text.split()))
        return max(0.5, round(words / 2.6, 2))  # ~156 wpm speaking rate

    async def synthesize(self, text: str, out_path: str | Path | None = None, *,
                         engine: str | None = None, voice: str | None = None,
                         effect: str | None = None,
                         strength: float | None = None) -> dict[str, Any]:
        """Synthesise ``text`` to a WAV file through the engine chain.

        Keyword overrides (``engine``, ``voice``, ``effect``, ``strength``) apply to
        this call only (used for auditions). With an explicit ``engine`` there is
        no fallback other than the null placeholder, so an audition never plays
        a different engine than the one asked for.

        Returns ``{"ok", "path", "engine", "duration_s", "reason", "speech",
        "attempts", "fallback_reason", "voice", "effect", "effect_strength",
        "sample_rate", "spoken_text"}``.
        """
        spoken = clean_for_speech(text or "")
        target = Path(out_path) if out_path is not None else _temp_wav()
        base: dict[str, Any] = {
            "ok": False, "path": None, "engine": engine or self.engine, "duration_s": 0.0,
            "speech": False, "reason": "", "attempts": [], "fallback_reason": "",
            "voice": None, "effect": "none", "effect_strength": 0.0,
            "sample_rate": None, "spoken_text": spoken,
        }
        if not spoken:
            base["reason"] = "empty text"
            return base
        return await asyncio.to_thread(self._synthesize_sync, spoken, target, base,
                                       engine, voice, effect, strength)

    def _synthesize_sync(self, spoken: str, target: Path, result: dict[str, Any],
                         engine: str | None, voice: str | None, effect: str | None,
                         strength: float | None) -> dict[str, Any]:
        if engine is not None:
            name = str(engine).strip().lower()
            if name not in ENGINES:
                result["reason"] = f"unknown engine '{engine}'"
                return result
            chain = [name] if name == "null" else [name, "null"]
        else:
            chain = self.chain
        fx = normalise_effect(effect if effect is not None else self.effect)
        amount = _clamp01(strength, self.effect_strength) if strength is not None \
            else self.effect_strength
        attempts: list[dict[str, Any]] = result["attempts"]

        for name in chain:
            if name == "null":
                break
            ok, reason = self.probe_engine(name)
            if not ok:
                attempts.append({"engine": name, "ok": False, "reason": reason})
                continue
            eng = self.engines[name]
            try:
                # A voice override names a voice of the requested/primary engine;
                # a fallback engine uses its own configured voice.
                v_arg = voice if (engine is not None or name == chain[0]) else None
                samples, sr, detail = eng.synth(spoken, v_arg)
                processed = apply_effect(samples, sr, fx, amount)
                seconds = write_wav(target, processed, sr)
            except Exception as exc:
                log.warning("%s synthesis failed: %s", name, exc)
                attempts.append({"engine": name, "ok": False,
                                 "reason": f"{name} synthesis failed: {exc}"})
                continue
            if seconds <= 0.0:
                attempts.append({"engine": name, "ok": False, "reason": "no audio produced"})
                continue
            attempts.append({"engine": name, "ok": True, "reason": ""})
            skipped = [a["reason"] for a in attempts if not a["ok"]]
            result.update({
                "ok": True, "path": str(target), "engine": name,
                "duration_s": round(seconds, 3), "speech": True,
                "reason": "", "fallback_reason": "; ".join(skipped),
                "voice": detail.get("voice"), "effect": fx,
                "effect_strength": amount if fx != "none" else 0.0,
                "sample_rate": int(sr), "detail": detail,
            })
            return result

        # Null placeholder: explicit, never claimed as speech.
        skipped = [a["reason"] for a in attempts if not a["ok"]]
        seconds = _write_silence(target, self._estimate_seconds(spoken), self.sample_rate)
        attempts.append({"engine": "null", "ok": False, "reason": "silent placeholder"})
        result.update({
            "ok": False, "path": str(target), "engine": "null",
            "duration_s": round(seconds, 3), "speech": False,
            "fallback_reason": "; ".join(skipped),
            "sample_rate": self.sample_rate,
            "reason": ("null engine: wrote a silent placeholder; no speech"
                       + (f" ({'; '.join(skipped)})" if skipped else "")),
        })
        return result
