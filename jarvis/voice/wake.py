"""Wake-word detection via openwakeword (optional).

openwakeword, onnxruntime and numpy are imported lazily. The detector uses the
**ONNX** build of openwakeword's built-in pretrained model named by
``config.voice.wake_model`` (default ``hey_jarvis``) plus the two shared feature
models (melspectrogram, embedding). Nothing is ever downloaded here: when the
model files are not present in the openwakeword package the probe reports
``available=False`` with the exact files missing and how the owner can fetch
them. A configured value that is a path to an ``.onnx`` file is used as-is.

There is deliberately no "load every pretrained model" fallback: that would
make unrelated phrases ("alexa", "hey mycroft", "timer"...) wake JARVIS.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("jarvis.voice.wake")

__all__ = ["WakeWord", "DEFAULT_WAKE_MODEL"]

# openwakeword ships this pretrained model out of the box.
DEFAULT_WAKE_MODEL = "hey_jarvis"


def _voice(config: Any) -> Any:
    return getattr(config, "voice", config) if config is not None else None


def _numpy() -> Any:
    try:
        import numpy as np
    except Exception:  # pragma: no cover - optional dependency
        return None
    return np


class WakeWord:
    """Thin wrapper over an openwakeword model."""

    def __init__(self, config: Any = None, *, model_name: str | None = None,
                 threshold: float = 0.5) -> None:
        v = _voice(config)
        configured = ""
        if v is not None:
            configured = (getattr(v, "wake_model", "")
                          or getattr(v, "wakeword_model", "") or "")
        self.model_name = str(model_name or configured or DEFAULT_WAKE_MODEL)
        try:
            self.threshold = float(getattr(v, "wake_threshold", threshold) or threshold)
        except (TypeError, ValueError):
            self.threshold = float(threshold)
        self._model: Any = None
        self._probe: tuple[bool, str] | None = None

    # -- model files -----------------------------------------------------
    def model_files(self) -> tuple[Path | None, list[Path], str]:
        """``(wake_model_path, feature_model_paths, reason)`` for the ONNX build."""
        direct = Path(self.model_name)
        try:
            import openwakeword  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            return None, [], f"openwakeword not installed ({exc.__class__.__name__})"
        res = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        features = [res / "melspectrogram.onnx", res / "embedding_model.onnx"]
        if direct.suffix == ".onnx":
            return (direct if direct.is_file() else None), features, (
                "" if direct.is_file() else f"wake model file not found: {direct}")
        key = self.model_name.replace(" ", "_")
        spec = getattr(openwakeword, "MODELS", {}).get(key)
        if spec is None:
            return None, features, (f"'{self.model_name}' is not an openwakeword built-in "
                                    f"model (known: {', '.join(getattr(openwakeword, 'MODELS', {}))})")
        model = Path(str(spec["model_path"]).replace(".tflite", ".onnx"))
        return model, features, ""

    # -- availability ----------------------------------------------------
    def _probe_impl(self) -> tuple[bool, str]:
        if _numpy() is None:
            return False, "numpy is required for wake-word detection"
        try:
            import openwakeword  # noqa: F401
        except Exception as exc:  # pragma: no cover - optional dependency
            return False, f"openwakeword not installed ({exc.__class__.__name__}: {exc})"
        try:
            import onnxruntime  # noqa: F401
        except Exception as exc:  # pragma: no cover - optional dependency
            return False, (f"openwakeword present but onnxruntime missing "
                           f"({exc.__class__.__name__})")
        model, features, reason = self.model_files()
        if model is None:
            return False, reason
        missing = [p.name for p in [model, *features] if not p.is_file()]
        if missing:
            return False, ("openwakeword model files not downloaded: " + ", ".join(missing)
                           + " (owner action: python -c \"import openwakeword.utils as u; "
                           f"u.download_models(['{self.model_name}'])\" fetches them from "
                           "github.com/dscripka/openWakeWord releases)")
        return True, ""

    async def available(self) -> tuple[bool, str]:
        """``(available, reason)`` — cached and cheap."""
        if self._probe is None:
            self._probe = await asyncio.to_thread(self._probe_impl)
        return self._probe

    # -- model -----------------------------------------------------------
    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        from openwakeword.model import Model
        model, _features, reason = self.model_files()
        if model is None:
            raise RuntimeError(reason)
        # ONNX explicitly: tflite-runtime is not available on Windows.
        self._model = Model(wakeword_models=[str(model)], inference_framework="onnx")
        log.info("wake word model loaded: %s", model.name)
        return self._model

    # -- inference -------------------------------------------------------
    def feed(self, frame: Any) -> bool:
        """Feed one 16 kHz mono frame; True when the wake word fires.

        ``frame`` may be a float32 numpy array (values in ``[-1, 1]``), a list of
        floats, or raw int16 bytes. Returns ``False`` whenever the detector is
        unavailable rather than raising.
        """
        if self._probe is None:
            self._probe = self._probe_impl()
        if not self._probe[0]:
            return False
        np = _numpy()
        if np is None:
            return False
        try:
            arr = np.asarray(frame, dtype=np.float32)
            if arr.ndim > 1:
                arr = arr[:, 0]
            arr = np.clip(arr, -1.0, 1.0)
            pcm = (arr * 32767.0).astype(np.int16)
            model = self._load_model()
            scores = model.predict(pcm)
        except Exception as exc:  # pragma: no cover - inference failure
            log.warning("wake-word inference failed: %s", exc)
            return False
        if not scores:
            return False
        try:
            return any(float(v) >= self.threshold for v in scores.values())
        except Exception:  # pragma: no cover - unexpected score shape
            return False

    def reset(self) -> None:
        """Clear the model's internal audio buffer between utterances."""
        if self._model is not None:
            try:
                self._model.reset()
            except Exception:  # pragma: no cover - defensive
                pass
