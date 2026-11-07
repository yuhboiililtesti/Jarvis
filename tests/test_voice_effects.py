"""Oracles for the "jarvis" audio effect chain (synthetic signals, no model)."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from jarvis.voice.effects import CEILING, TAIL_S, apply_effect  # noqa: E402

SR = 24000


def _sine(freq: float, amp: float, seconds: float = 1.0, sr: int = SR):
    t = np.arange(int(sr * seconds)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _noise(seconds: float = 1.0, sr: int = SR, seed: int = 7):
    rng = np.random.default_rng(seed)
    return np.clip(rng.standard_normal(int(sr * seconds)), -1.0, 1.0).astype(np.float32)


def _gain_db(freq: float, strength: float) -> float:
    """Steady-state gain at ``freq`` for a quiet tone (below the compressor)."""
    x = _sine(freq, 0.01)
    y = apply_effect(x, SR, "jarvis", strength)
    mid = slice(SR // 4, 3 * SR // 4)
    return 20 * np.log10(np.sqrt(np.mean(y[mid] ** 2)) / np.sqrt(np.mean(x[mid] ** 2)))


def test_none_is_bit_exact_passthrough():
    x = _noise()
    y = apply_effect(x, SR, "none", 0.9)
    assert y is x
    assert np.array_equal(y, _noise())


def test_unknown_effect_name_is_passthrough():
    x = _sine(440, 0.5)
    assert apply_effect(x, SR, "reverb-cathedral", 1.0) is x


@pytest.mark.parametrize("strength", [0.0, 0.25, 0.5, 1.0])
@pytest.mark.parametrize("kind", ["sine", "noise", "clipped-square"])
def test_never_exceeds_minus_one_dbfs(kind, strength):
    if kind == "sine":
        x = _sine(440, 1.0)
    elif kind == "noise":
        x = _noise()
    else:
        x = np.sign(_sine(220, 1.0)).astype(np.float32) * 1.5   # over full scale
    y = apply_effect(x, SR, "jarvis", strength)
    assert float(np.max(np.abs(y))) <= CEILING + 1e-7
    assert CEILING == pytest.approx(10 ** (-1 / 20), rel=1e-9)
    assert y.dtype == np.float32
    assert np.all(np.isfinite(y))


@pytest.mark.parametrize("strength", [0.0, 0.5, 1.0])
def test_length_grows_by_at_most_the_reverb_tail(strength):
    x = _noise(0.8)
    y = apply_effect(x, SR, "jarvis", strength)
    assert len(x) <= len(y) <= len(x) + int(TAIL_S * SR)
    if strength == 0.0:
        assert len(y) == len(x)      # no reverb at zero strength


def test_deterministic():
    x = _noise(0.5)
    a = apply_effect(x, SR, "jarvis", 0.6)
    b = apply_effect(x.copy(), SR, "jarvis", 0.6)
    assert np.array_equal(a, b)


def test_high_pass_removes_rumble():
    # 2nd-order Butterworth at 90 Hz: a 40 Hz tone loses well over 9 dB
    # relative to 1 kHz, even at strength 0 (the high-pass is cleanup, always on).
    for s in (0.0, 1.0):
        assert _gain_db(40, s) - _gain_db(1000, s) < -9.0


def test_presence_lift_scales_with_strength():
    flat = _gain_db(3200, 0.0) - _gain_db(1000, 0.0)
    lifted = _gain_db(3200, 1.0) - _gain_db(1000, 1.0)
    assert abs(flat) < 0.5
    assert lifted - flat >= 3.0


def test_edge_inputs():
    assert len(apply_effect(np.zeros(0, dtype=np.float32), SR, "jarvis", 0.5)) == 0
    bad = _sine(440, 0.5)
    bad[100] = np.nan
    bad[200] = np.inf
    y = apply_effect(bad, SR, "jarvis", 0.5)
    assert np.all(np.isfinite(y))
    stereo = np.stack([_sine(440, 0.5), _sine(440, 0.5)], axis=1)
    assert apply_effect(stereo, SR, "jarvis", 0.5).ndim == 1


def test_works_at_piper_and_whisper_rates():
    for sr in (16000, 22050):
        x = _sine(440, 1.0, sr=sr)
        y = apply_effect(x, sr, "jarvis", 0.7)
        assert float(np.max(np.abs(y))) <= CEILING + 1e-7
        assert len(x) <= len(y) <= len(x) + int(TAIL_S * sr)
