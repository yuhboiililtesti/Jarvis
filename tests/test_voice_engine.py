"""TTS engine chain, voice pipeline and wake-model resolution, with fakes only.

No model, no audio device, no network: engines, mic, transcriber, wake word
and agent are replaced by small fakes. A real Kokoro synthesis test exists but
only runs when the model files are present AND JARVIS_TEST_REAL_TTS=1.
"""

from __future__ import annotations

import asyncio
import os
import wave
from types import SimpleNamespace

import pytest

from jarvis.config import Config

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from jarvis.voice.effects import CEILING  # noqa: E402
from jarvis.voice.tts import KokoroEngine, Speaker  # noqa: E402

SR = 24000


class FakeEngine:
    def __init__(self, name, *, ok=True, reason="", fail=None, amp=0.5, seconds=0.5):
        self.name = name
        self.ok = ok
        self.reason = reason
        self.fail = fail
        self.amp = amp
        self.seconds = seconds
        self.calls: list[tuple[str, str | None]] = []

    def probe(self):
        return self.ok, self.reason

    def synth(self, text, voice=None):
        self.calls.append((text, voice))
        if self.fail:
            raise RuntimeError(self.fail)
        t = np.arange(int(SR * self.seconds)) / SR
        return (self.amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32), SR, {
            "voice": voice or f"{self.name}-default"}


def _speaker(tmp_path, engines, *, engine="kokoro", fallback=("piper",), effect="none"):
    cfg = Config()
    cfg.data_dir = str(tmp_path / "data")
    cfg.voice.tts_engine = engine
    cfg.voice.tts_fallback = list(fallback)
    cfg.voice.effect = effect
    return Speaker(cfg, engines=engines)


def _read(path):
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0, w.getframerate()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# chain
# ---------------------------------------------------------------------------

def test_chain_order_dedup_and_null_last(tmp_path):
    sp = _speaker(tmp_path, {}, engine="piper", fallback=["null", "kokoro", "piper"])
    assert sp.chain == ["piper", "kokoro", "null"]


def test_primary_used_when_available(tmp_path):
    kok, pip = FakeEngine("kokoro"), FakeEngine("piper")
    sp = _speaker(tmp_path, {"kokoro": kok, "piper": pip})
    res = run(sp.synthesize("Good evening, sir.", tmp_path / "a.wav"))
    assert res["ok"] and res["speech"] and res["engine"] == "kokoro"
    assert res["fallback_reason"] == ""
    assert len(kok.calls) == 1 and not pip.calls
    data, sr = _read(res["path"])
    assert sr == SR and abs(len(data) / SR - 0.5) < 0.01


def test_unavailable_primary_falls_back_with_reason(tmp_path):
    kok = FakeEngine("kokoro", ok=False, reason="kokoro model missing: X")
    pip = FakeEngine("piper")
    sp = _speaker(tmp_path, {"kokoro": kok, "piper": pip})
    res = run(sp.synthesize("Hello.", tmp_path / "b.wav"))
    assert res["ok"] and res["engine"] == "piper"
    assert "kokoro model missing" in res["fallback_reason"]
    assert [a["engine"] for a in res["attempts"]] == ["kokoro", "piper"]
    assert not kok.calls
    ok, reason = run(sp.available())
    assert ok and "using piper" in reason and "kokoro unavailable" in reason


def test_primary_synthesis_error_falls_back(tmp_path):
    kok = FakeEngine("kokoro", fail="onnx exploded")
    pip = FakeEngine("piper")
    sp = _speaker(tmp_path, {"kokoro": kok, "piper": pip})
    res = run(sp.synthesize("Hello.", tmp_path / "c.wav"))
    assert res["engine"] == "piper" and res["ok"]
    assert "onnx exploded" in res["fallback_reason"]


def test_everything_fails_gives_honest_null_placeholder(tmp_path):
    sp = _speaker(tmp_path, {"kokoro": FakeEngine("kokoro", ok=False, reason="no kokoro"),
                             "piper": FakeEngine("piper", fail="piper crashed")})
    res = run(sp.synthesize("Hello there.", tmp_path / "d.wav"))
    assert res["ok"] is False and res["speech"] is False and res["engine"] == "null"
    assert "no kokoro" in res["reason"] and "piper crashed" in res["reason"]
    data, _ = _read(res["path"])
    assert len(data) > 0 and not np.any(data)          # silent, and says so


def test_null_engine_configured(tmp_path):
    sp = _speaker(tmp_path, {}, engine="null", fallback=[])
    ok, reason = run(sp.available())
    assert not ok and "null" in reason
    res = run(sp.synthesize("Hello.", tmp_path / "e.wav"))
    assert res["engine"] == "null" and not res["speech"]


def test_explicit_engine_override_never_substitutes(tmp_path):
    kok, pip = FakeEngine("kokoro", ok=False, reason="absent"), FakeEngine("piper")
    sp = _speaker(tmp_path, {"kokoro": kok, "piper": pip})
    res = run(sp.synthesize("Audition.", tmp_path / "f.wav", engine="kokoro", voice="bm_lewis"))
    assert res["engine"] == "null" and not res["ok"]
    assert not pip.calls


def test_voice_override_only_reaches_its_engine(tmp_path):
    kok = FakeEngine("kokoro", fail="boom")
    pip = FakeEngine("piper")
    sp = _speaker(tmp_path, {"kokoro": kok, "piper": pip})
    run(sp.synthesize("Hi.", tmp_path / "g.wav", voice="bm_lewis"))
    assert kok.calls[0][1] == "bm_lewis"
    assert pip.calls[0][1] is None            # piper keeps its own voice


def test_empty_or_markdown_only_text(tmp_path):
    sp = _speaker(tmp_path, {"kokoro": FakeEngine("kokoro")})
    assert run(sp.synthesize("   "))["reason"] == "empty text"
    res = run(sp.synthesize("**Done**, sir. See https://example.com.", tmp_path / "h.wav"))
    assert res["spoken_text"] == "Done, sir. See a link to example.com."


def test_effect_is_applied_and_never_clips(tmp_path):
    kok = FakeEngine("kokoro", amp=1.0)
    sp = _speaker(tmp_path, {"kokoro": kok}, effect="jarvis")
    res = run(sp.synthesize("Loud.", tmp_path / "i.wav"))
    assert res["effect"] == "jarvis" and res["effect_strength"] == 0.5
    data, _ = _read(res["path"])
    assert float(np.max(np.abs(data))) <= CEILING + 1e-3
    res2 = run(sp.synthesize("Loud.", tmp_path / "j.wav", effect="none"))
    data2, _ = _read(res2["path"])
    assert float(np.max(np.abs(data2))) > 0.99             # untouched full scale
    assert res2["effect_strength"] == 0.0


def test_configure_validates_and_resets_probe(tmp_path):
    kok = FakeEngine("kokoro")
    sp = _speaker(tmp_path, {"kokoro": kok, "piper": FakeEngine("piper")})
    assert sp.configure(engine="kokoro", voice="bm_daniel", effect="none", strength=2)[
        "effect_strength"] == 1.0
    assert sp.voice_name == "bm_daniel"
    for bad in ({"engine": "elevenlabs"}, {"effect": "robot"}, {"voice": "../evil"}):
        with pytest.raises(ValueError):
            sp.configure(**bad)
    with pytest.raises(ValueError):
        sp.configure(engine="piper", voice="not-installed-voice")
    sp.probe_engine("kokoro")
    kok.ok, kok.reason = False, "now missing"
    sp.configure()                                       # clears the probe cache
    assert sp.probe_engine("kokoro") == (False, "now missing")


def test_kokoro_probe_reports_missing_files(tmp_path):
    cfg = Config()
    cfg.data_dir = str(tmp_path / "data")
    sp = Speaker(cfg)
    ok, reason = KokoroEngine(sp).probe()
    assert not ok
    assert ("not installed" in reason) or ("fetch-voices.ps1" in reason)
    voices = sp.list_voices()
    assert [v["id"] for v in voices["kokoro"]["voices"]] == \
        ["bm_george", "bm_lewis", "bm_daniel", "bm_fable"]
    assert voices["kokoro"]["installed"] is False


@pytest.mark.skipif(os.environ.get("JARVIS_TEST_REAL_TTS") != "1",
                    reason="real Kokoro synthesis only with JARVIS_TEST_REAL_TTS=1")
def test_real_kokoro_bm_george(tmp_path):  # pragma: no cover - opt-in
    cfg = Config()
    sp = Speaker(cfg)
    if not KokoroEngine(sp).installed():
        pytest.skip("kokoro model files not present")
    res = run(sp.synthesize("Good evening, sir.", tmp_path / "k.wav"))
    assert res["ok"] and res["engine"] == "kokoro" and res["duration_s"] > 0.5


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

class FakePlayer:
    def __init__(self):
        self.played: list[str] = []

    def play_file(self, path):
        self.played.append(str(path))
        return {"ok": True, "reason": ""}


class FakeAgent:
    def __init__(self, reply="Certainly, sir. It is done."):
        self.reply = reply
        self.calls: list[dict] = []

    async def complete(self, text, *, session=None, source="text", user="owner"):
        self.calls.append({"text": text, "source": source})
        return self.reply


class FakeTranscriber:
    model_name, device, compute_type = "fake", "cpu", "int8"

    def __init__(self, texts):
        self.texts = list(texts)
        self.lengths: list[int] = []

    async def available(self):
        return True, ""

    async def transcribe(self, data, language=None, *, sample_rate=16000):
        self.lengths.append(len(data))
        text = self.texts.pop(0) if self.texts else ""
        return {"ok": True, "text": text, "language": "en", "duration": 0.0,
                "segments": [], "reason": ""}


class FakeMic:
    frame_ms = 30

    def __init__(self, frames):
        self.frames = list(frames)

    def get_frame(self, timeout=1.0):
        return self.frames.pop(0) if self.frames else None

    def stop(self):
        pass


def _service(tmp_path, *, agent=None):
    from jarvis.voice.pipeline import VoiceService
    cfg = Config()
    cfg.data_dir = str(tmp_path / "data")
    cfg.voice.effect = "none"
    cfg.voice.conversation_timeout_s = 1.0
    jarvis = SimpleNamespace(config=cfg, bus=None, agent=agent or FakeAgent(),
                             session=lambda sid=None: {"id": "default", "messages": []})
    svc = VoiceService(jarvis)
    svc.speaker = Speaker(cfg, engines={"kokoro": FakeEngine("kokoro", seconds=0.3)})
    svc.player = FakePlayer()
    return svc


def _frames(kind: str, count: int):
    n = 480                                        # 30 ms at 16 kHz
    if kind == "speech":
        t = np.arange(n) / 16000
        return [(0.3 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)] * count
    return [np.zeros(n, dtype=np.float32)] * count


def test_say_streams_sentences_and_plays_each(tmp_path):
    svc = _service(tmp_path)
    res = run(svc.say("Good evening, sir. All systems are online. Standing by."))
    assert res["ok"] and res["speech"] and res["played"]
    assert len(res["parts"]) == 3 and len(svc.player.played) == 3
    # joined file = 3 x 0.3 s + 2 x 0.12 s gaps
    assert res["duration_s"] == pytest.approx(0.9 + 0.24, abs=0.02)
    assert res["first_audio_s"] is not None


def test_say_muted_or_no_play_does_not_touch_the_speaker(tmp_path):
    svc = _service(tmp_path)
    svc.muted = True
    res = run(svc.say("Quiet, please."))
    assert res["ok"] and not res["played"] and "muted" in res["reason"]
    svc.muted = False
    res = run(svc.say("Quiet, please.", play=False))
    assert not res["played"] and not svc.player.played


def test_handle_utterance_uses_spoken_persona_mode(tmp_path):
    agent = FakeAgent()
    svc = _service(tmp_path, agent=agent)
    reply = run(svc.handle_utterance("what time is it"))
    assert reply == agent.reply
    assert agent.calls == [{"text": "what time is it", "source": "voice"}]
    assert len(svc.player.played) == 2                  # two reply sentences


def test_capture_utterance_is_vad_end_pointed(tmp_path):
    svc = _service(tmp_path)
    svc.transcriber = FakeTranscriber(["hello jarvis"])
    # 10 silent frames, 20 speech frames (0.6 s), then 40 silent frames.
    svc.mic = FakeMic(_frames("silence", 10) + _frames("speech", 20) + _frames("silence", 40))
    res = run(svc.capture_utterance(wait_s=2.0))
    assert res["text"] == "hello jarvis"
    captured_s = svc.transcriber.lengths[0] / 16000
    # speech (0.6 s) + pre-roll + VAD hang-over (~0.7 s), but not the whole buffer
    assert 0.6 <= captured_s <= 1.8
    assert len(svc.mic.frames) > 0                      # stopped at end of speech


def test_capture_utterance_times_out_on_silence(tmp_path):
    svc = _service(tmp_path)
    svc.transcriber = FakeTranscriber(["never"])
    svc.mic = FakeMic(_frames("silence", 100))
    res = run(svc.capture_utterance(wait_s=0.5))
    assert not res["ok"] and "timeout" in res["reason"]
    assert svc.transcriber.lengths == []


def test_conversation_takes_follow_ups_until_silence(tmp_path):
    agent = FakeAgent("Very good, sir.")
    svc = _service(tmp_path, agent=agent)
    svc.transcriber = FakeTranscriber(["first request", "follow up"])
    frames = (_frames("speech", 15) + _frames("silence", 30)
              + _frames("speech", 15) + _frames("silence", 30))

    class DrainAwareMic(FakeMic):
        """Frames for the second turn arrive after the first reply is spoken."""
        def __init__(self, first, second):
            super().__init__(first)
            self.second = second

        def get_frame(self, timeout=1.0):
            f = super().get_frame(timeout)
            if f is None and self.second is not None and timeout == 0.0:
                return None                         # drain sees an empty queue
            if f is None and self.second is not None:
                self.frames, self.second = self.second, None
                return super().get_frame(timeout)
            return f

    svc.mic = DrainAwareMic(frames[:45], frames[45:])
    turns = run(svc._conversation())
    assert turns == 2
    assert [c["text"] for c in agent.calls] == ["first request", "follow up"]
    assert all(c["source"] == "voice" for c in agent.calls)


def test_start_refuses_when_wake_disabled(tmp_path):
    svc = _service(tmp_path)
    svc.voice_config.wake_enabled = False
    res = run(svc.start())
    assert res["running"] is False and "wake word disabled" in res["reason"]
    assert not svc.is_listening()


def test_wake_model_resolution_is_explicit():
    pytest.importorskip("openwakeword")
    from jarvis.voice.wake import WakeWord
    cfg = Config()
    w = WakeWord(cfg)
    model, features, reason = w.model_files()
    assert w.model_name == "hey_jarvis"
    assert model is not None and model.name == "hey_jarvis_v0.1.onnx" and reason == ""
    assert {p.name for p in features} == {"melspectrogram.onnx", "embedding_model.onnx"}
    cfg.voice.wake_model = "hey_nobody"
    model, _f, reason = WakeWord(cfg).model_files()
    assert model is None and "not an openwakeword built-in" in reason
