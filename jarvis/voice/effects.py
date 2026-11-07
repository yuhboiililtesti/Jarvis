"""Deterministic post-processing that gives a stock TTS voice an "AI butler" sheen.

Only numpy + scipy. No voice cloning, no reference audio: the character comes
from ordinary studio processing applied to whatever stock voice synthesised the
speech.

``effect="jarvis"`` chain (``strength`` 0..1 scales every coloured stage):

1. high-pass, 2nd-order Butterworth at 90 Hz (rumble / plosive clean-up)
2. gentle low-mid dip (~280 Hz, up to -2 dB) to reduce boxiness
3. presence lift, peaking EQ at 3.2 kHz (up to +4.5 dB)
4. light feed-forward compressor (threshold -22 dBFS, ratio up to 3:1)
5. very faint "AI sheen": a high-passed, slowly modulated doubled copy
6. subtle short plate/room reverb (seeded synthetic impulse, ~0.3 s)
7. peak ceiling at -1 dBFS (gain is only ever *reduced* here)

``effect="none"`` returns the input array unchanged (bit-exact passthrough).

Guarantees (asserted by tests): output peak <= -1 dBFS for the jarvis chain,
length grows by at most ``TAIL_S`` seconds, identical input -> identical output.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

__all__ = ["EFFECTS", "CEILING_DBFS", "TAIL_S", "apply_effect", "normalise_effect"]

EFFECTS = ("jarvis", "none")
CEILING_DBFS = -1.0
CEILING = 10.0 ** (CEILING_DBFS / 20.0)   # ~0.891
TAIL_S = 0.30                              # max reverb tail appended
_IR_SEED = 0x4A415256                      # "JARV": fixed so output is reproducible


def normalise_effect(effect: Any) -> str:
    name = str(effect or "none").strip().lower()
    return name if name in EFFECTS else "none"


def _clamp01(value: Any, default: float = 0.5) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(v):
        return default
    return max(0.0, min(1.0, v))


# ---------------------------------------------------------------------------
# filters (RBJ audio-EQ cookbook biquads as second-order sections)
# ---------------------------------------------------------------------------

def _peaking_sos(np: Any, f0: float, q: float, gain_db: float, sr: int) -> Any:
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / sr
    alpha = math.sin(w0) / (2.0 * q)
    cw = math.cos(w0)
    b0, b1, b2 = 1 + alpha * a, -2 * cw, 1 - alpha * a
    a0, a1, a2 = 1 + alpha / a, -2 * cw, 1 - alpha / a
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


@lru_cache(maxsize=16)
def _plate_ir(sr: int) -> Any:
    """Seeded synthetic plate impulse: pre-delay + decaying filtered noise."""
    import numpy as np
    from scipy import signal

    length = int(TAIL_S * sr)
    rng = np.random.default_rng(_IR_SEED)
    noise = rng.standard_normal(length).astype(np.float64)
    t = np.arange(length) / sr
    rt60 = 0.28
    env = np.exp(-6.907755 * t / rt60)            # -60 dB at rt60
    pre = int(0.009 * sr)
    env[:pre] = 0.0
    ir = noise * env
    # Darken and thin the tail so it reads as a small bright plate, not a hall.
    sos = signal.butter(2, [350.0, min(6500.0, 0.45 * sr)], btype="bandpass",
                        fs=sr, output="sos")
    ir = signal.sosfilt(sos, ir)
    ir /= max(1e-9, float(np.sqrt(np.sum(ir * ir))))   # unit energy
    return ir


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------

def _compress(np: Any, signal: Any, x: Any, sr: int, amount: float) -> Any:
    """Smooth feed-forward compressor; deterministic, vectorised."""
    if amount <= 0.0:
        return x
    thr = 10.0 ** (-22.0 / 20.0)
    ratio = 1.0 + 2.0 * amount
    # Envelope: rectified signal through a ~8 ms one-pole smoother.
    a = 1.0 - math.exp(-1.0 / (0.008 * sr))
    env = signal.lfilter([a], [1.0, a - 1.0], np.abs(x))
    env = np.maximum(env, 1e-9)
    gain = np.where(env > thr, (env / thr) ** (1.0 / ratio - 1.0), 1.0)
    # Smooth the gain itself (~40 ms) to avoid zipper noise.
    g = 1.0 - math.exp(-1.0 / (0.040 * sr))
    gain = signal.lfilter([g], [1.0, g - 1.0], gain, zi=[gain[0] * (1.0 - g)])[0]
    makeup = 10.0 ** ((2.5 * amount) / 20.0)
    return x * gain * makeup


def _sheen(np: Any, signal: Any, x: Any, sr: int, amount: float) -> Any:
    """Faint doubled copy: high-passed, ~14 ms delay with slow 0.35 Hz wobble."""
    if amount <= 0.0:
        return np.zeros_like(x)
    n = len(x)
    idx = np.arange(n, dtype=np.float64)
    delay = (0.014 + 0.0025 * np.sin(2.0 * math.pi * 0.35 * idx / sr)) * sr
    pos = idx - delay
    wet = np.interp(pos, idx, x, left=0.0, right=0.0)
    sos = signal.butter(2, 1800.0, btype="highpass", fs=sr, output="sos")
    wet = signal.sosfilt(sos, wet)
    return wet * (0.16 * amount)


def apply_effect(samples: Any, sample_rate: int, effect: str = "jarvis",
                 strength: float = 0.5) -> Any:
    """Apply ``effect`` to mono float audio and return float32 samples.

    ``effect="none"`` returns ``samples`` itself, untouched.
    """
    name = normalise_effect(effect)
    if name == "none":
        return samples

    import numpy as np
    from scipy import signal

    sr = int(sample_rate)
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0).reshape(-1)
    n = len(x)
    if n == 0 or sr <= 0:
        return x.astype(np.float32)
    s = _clamp01(strength)

    # 1. high-pass (always on in the jarvis chain: it is cleanup, not colour)
    hp = signal.butter(2, 90.0, btype="highpass", fs=sr, output="sos")
    y = signal.sosfilt(hp, x)

    # 2-3. tone shaping
    if s > 0.0:
        eq = np.vstack([
            _peaking_sos(np, 280.0, 1.0, -2.0 * s, sr),
            _peaking_sos(np, min(3200.0, 0.4 * sr), 0.8, 4.5 * s, sr),
        ])
        y = signal.sosfilt(eq, y)

    # 4. compression
    y = _compress(np, signal, y, sr, s)

    # 5. sheen (added on top of the dry path)
    if s > 0.0:
        y = y + _sheen(np, signal, y, sr, s)

    # 6. reverb (adds at most TAIL_S of tail)
    if s > 0.0:
        ir = _plate_ir(sr)
        wet = signal.fftconvolve(y, ir, mode="full")
        tail = len(ir) - 1
        y = np.concatenate([y, np.zeros(tail)]) + wet * (0.11 * s)
        # Fade the last 30 ms of the tail so truncation can never click.
        fade = min(int(0.03 * sr), tail)
        if fade > 0:
            y[-fade:] *= np.linspace(1.0, 0.0, fade)
        # Drop a tail that has decayed below -70 dBFS (keeps files tight).
        quiet = 10.0 ** (-70.0 / 20.0)
        loud = np.nonzero(np.abs(y[n:]) > quiet)[0]
        y = y[: n + (int(loud[-1]) + 1 if len(loud) else 0)]

    # 7. ceiling: only ever reduce gain, never boost.
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak > CEILING:
        y = y * (CEILING / peak)
    out = y.astype(np.float32)
    # float32 rounding can nudge a sample over the ceiling; clamp the last ulp.
    np.clip(out, -CEILING, CEILING, out=out)
    return out
