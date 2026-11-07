"""Voice HTTP routes against a tiny app that only includes the voice router.

Fake engines, fake transcriber, fake player, fake agent: no model, no audio
device, no network. The fake player guarantees nothing is ever played.
"""

from __future__ import annotations

import io
import time
import wave
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from jarvis.api.voice_routes import SAMPLE_LINE, AudioCache, build_voice_router  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.voice.tts import Speaker  # noqa: E402

SR = 24000


class FakeEngine:
    name = "kokoro"

    def __init__(self):
        self.calls = []

    def probe(self):
        return True, ""

    def synth(self, text, voice=None):
        self.calls.append((text, voice))
        t = np.arange(int(SR * 0.4)) / SR
        return (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), SR, {
            "voice": voice or "bm_george"}


class FakeTranscriber:
    model_name, device, compute_type = "fake", "cpu", "int8"

    def __init__(self, text="what is the weather", ok=True, reason=""):
        self.text, self.ok, self.reason = text, ok, reason
        self.received = []

    async def available(self):
        return self.ok, self.reason

    async def transcribe(self, data, language=None, *, sample_rate=16000):
        self.received.append((len(data), sample_rate))
        return {"ok": True, "text": self.text, "language": "en",
                "duration": len(data) / sample_rate, "segments": [], "reason": ""}


class FakePlayer:
    def __init__(self):
        self.played = []

    def play_file(self, path):
        self.played.append(path)
        return {"ok": True, "reason": ""}


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def complete(self, text, *, session=None, source="text", user="owner"):
        self.calls.append({"text": text, "source": source, "session": session})
        return "The weather is fine, sir. Shall I fetch an umbrella anyway?"


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, topic, **data):
        self.events.append((topic, data))


@pytest.fixture
def env(tmp_path):
    from jarvis.voice.pipeline import VoiceService
    cfg = Config()
    cfg.data_dir = str(tmp_path / "data")
    cfg.voice.effect = "jarvis"
    sessions = {}
    kernel = SimpleNamespace(
        config=cfg, bus=Bus(), agent=FakeAgent(), db=None,
        session=lambda sid=None: sessions.setdefault(sid or "default", {"id": sid}))
    svc = VoiceService(kernel)
    engine = FakeEngine()
    svc.speaker = Speaker(cfg, engines={"kokoro": engine})
    svc.transcriber = FakeTranscriber()
    svc.player = FakePlayer()
    kernel.services = {"voice": svc}
    cache = AudioCache(max_items=8, ttl_s=600)
    app = FastAPI()
    app.include_router(build_voice_router(kernel, cache=cache))
    client = TestClient(app)
    return SimpleNamespace(client=client, svc=svc, kernel=kernel, engine=engine,
                           cache=cache, cfg=cfg)


def _wav_bytes(seconds=1.0, sr=16000, speech=True):
    t = np.arange(int(sr * seconds)) / sr
    sig = np.zeros_like(t)
    if speech:
        mid = (t > 0.3) & (t < 0.7)
        sig[mid] = 0.3 * np.sin(2 * np.pi * 300 * t[mid])
    pcm = (sig * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


# ---------------------------------------------------------------- tts + audio

def test_tts_returns_servable_wav(env):
    r = env.client.post("/api/tts", json={"text": "Good evening, sir."})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["speech"] and body["engine"] == "kokoro"
    assert body["effect"] == "jarvis" and body["duration_s"] > 0.3
    assert body["url"].startswith("/api/voice/audio/") and len(body["id"]) >= 22
    assert body["server_playback"].startswith("skipped (output=browser)")
    assert env.svc.player.played == []                     # browser plays it
    audio = env.client.get(body["url"])
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert audio.headers["cache-control"] == "no-store"
    assert audio.content[:4] == b"RIFF" and audio.content[8:12] == b"WAVE"
    topics = [t for t, _ in env.kernel.bus.events]
    assert "voice.tts.done" in topics


def test_tts_plays_on_server_only_when_configured(env):
    env.cfg.voice.output = "server"
    r = env.client.post("/api/tts", json={"text": "Testing the server speaker."})
    assert r.json()["server_playback"] == "started"
    deadline = time.time() + 5
    while not env.svc.player.played and time.time() < deadline:
        time.sleep(0.02)
    assert len(env.svc.player.played) == 1
    env.svc.muted = True
    r = env.client.post("/api/tts", json={"text": "Muted now."})
    assert r.json()["server_playback"] == "skipped (muted)"


def test_tts_input_validation_and_disabled(env):
    assert env.client.post("/api/tts", json={"text": "  "}).status_code == 400
    assert env.client.post("/api/tts", json={"text": "x" * 5000}).status_code == 413
    env.cfg.voice.tts_enabled = False
    body = env.client.post("/api/tts", json={"text": "hello"}).json()
    assert body["ok"] is False and "disabled" in body["reason"] and body["url"] is None


@pytest.mark.parametrize("bad", ["nope", "../../etc/passwd", "..%2F..%2Fwin.ini",
                                 "A" * 21, "A" * 80, "%00" * 30])
def test_audio_ids_are_not_paths(env, bad):
    r = env.client.get(f"/api/voice/audio/{bad}")
    assert r.status_code == 404


def test_audio_cache_evicts_and_expires(tmp_path):
    cache = AudioCache(max_items=2, ttl_s=0.2)
    files = []
    for i in range(3):
        p = tmp_path / f"{i}.wav"
        p.write_bytes(b"RIFF")
        files.append((p, cache.put(p)))
    assert not files[0][0].exists()                 # evicted and deleted
    assert cache.get(files[0][1]) is None
    assert cache.get(files[2][1]) == files[2][0]
    time.sleep(0.3)
    assert cache.get(files[2][1]) is None           # expired
    assert not files[2][0].exists()
    assert cache.get("../" + "a" * 30) is None


# ---------------------------------------------------------------- voices/select/audition

def test_voices_lists_british_kokoro_and_active(env):
    body = env.client.get("/api/voice/voices").json()
    ids = [v["id"] for v in body["engines"]["kokoro"]["voices"]]
    assert ids == ["bm_george", "bm_lewis", "bm_daniel", "bm_fable"]
    assert body["active"]["engine"] == "kokoro" and body["active"]["voice"] == "bm_george"
    assert body["effects"] == ["jarvis", "none"]
    assert body["conversation_timeout_s"] == 8.0
    assert body["wake"]["enabled"] is False and body["wake"]["listening"] is False
    assert body["push_to_talk_hotkey"] == "ctrl+alt+space"


def test_select_switches_voice_and_mirrors_config(env):
    r = env.client.post("/api/voice/select",
                        json={"voice": "bm_lewis", "effect": "none", "effect_strength": 0.8})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == {"voice": "bm_lewis", "effect": "none", "effect_strength": 0.8}
    assert env.cfg.voice.kokoro_voice == "bm_lewis" and env.cfg.voice.effect == "none"
    assert env.svc.speaker.kokoro_voice == "bm_lewis"
    assert env.client.get("/api/voice/voices").json()["active"]["voice"] == "bm_lewis"
    assert env.client.post("/api/voice/select", json={"engine": "cloud"}).status_code == 400
    assert env.client.post("/api/voice/select", json={"output": "radio"}).status_code == 400
    assert env.client.post("/api/voice/select", json={"output": "both"}).json()[
        "applied"] == {"output": "both"}


def test_audition_does_not_change_selection(env):
    body = env.client.post("/api/voice/audition",
                           json={"voice": "bm_fable", "effect": "none"}).json()
    assert body["ok"] and body["text"] == SAMPLE_LINE and body["url"]
    assert env.engine.calls[-1][1] == "bm_fable"
    assert env.svc.speaker.kokoro_voice == "bm_george"
    assert env.svc.player.played == []


# ---------------------------------------------------------------- transcribe

def test_transcribe_wav_upload(env):
    r = env.client.post("/api/voice/transcribe",
                        files={"file": ("ptt.wav", _wav_bytes(), "audio/wav")})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "text": "what is the weather", "language": "en",
                    "duration": 1.0, "reason": ""}
    n, sr = env.svc.transcriber.received[0]
    assert sr == 16000 and n < 16000                 # VAD-trimmed, 16 kHz


def test_transcribe_resamples_to_16k(env):
    env.client.post("/api/voice/transcribe",
                    files={"file": ("a.wav", _wav_bytes(sr=48000), "audio/wav")})
    n, sr = env.svc.transcriber.received[0]
    assert sr == 16000 and 4000 < n < 16000


def test_transcribe_silence_and_unavailable_and_limits(env, monkeypatch):
    body = env.client.post("/api/voice/transcribe",
                           files={"file": ("s.wav", _wav_bytes(speech=False), "audio/wav")}).json()
    assert not body["ok"] and "no speech" in body["reason"]
    assert env.client.post("/api/voice/transcribe").status_code == 422
    assert env.client.post("/api/voice/transcribe",
                           files={"file": ("x.html", b"<html>", "text/html")}).status_code == 415
    import jarvis.voice.decode as decode
    monkeypatch.setattr(decode, "MAX_UPLOAD_BYTES", 1000)
    assert env.client.post("/api/voice/transcribe",
                           files={"file": ("big.wav", _wav_bytes(), "audio/wav")}).status_code == 413
    monkeypatch.undo()
    env.svc.transcriber = FakeTranscriber(ok=False, reason="faster-whisper not installed")
    body = env.client.post("/api/voice/transcribe",
                           files={"file": ("a.wav", _wav_bytes(), "audio/wav")}).json()
    assert not body["ok"] and body["reason"] == "faster-whisper not installed"


def test_transcribe_garbage_reports_decode_failure(env):
    body = env.client.post("/api/voice/transcribe",
                           files={"file": ("a.ogg", b"not audio at all" * 10,
                                           "audio/ogg")}).json()
    assert not body["ok"] and "could not decode" in body["reason"]


# ---------------------------------------------------------------- converse / mic

def test_converse_runs_spoken_turn(env):
    r = env.client.post("/api/voice/converse",
                        files={"file": ("u.wav", _wav_bytes(), "audio/wav")},
                        data={"session_id": "hud"})
    body = r.json()
    assert body["ok"] and body["transcript"] == "what is the weather"
    assert body["reply"].startswith("The weather is fine")
    assert body["audio"]["url"].startswith("/api/voice/audio/")
    call = env.kernel.agent.calls[0]
    assert call["source"] == "voice" and call["session"]["id"] == "hud"


def test_converse_when_nothing_heard(env):
    body = env.client.post("/api/voice/converse",
                           files={"file": ("s.wav", _wav_bytes(speech=False), "audio/wav")}).json()
    assert not body["ok"] and "no speech" in body["reason"]
    assert env.kernel.agent.calls == []


def test_mic_stop_is_safe_when_not_running(env):
    body = env.client.post("/api/voice/mic/stop").json()
    assert body == {"ok": True, "was_running": False, "running": False}


def test_missing_voice_service_is_503():
    app = FastAPI()
    app.include_router(build_voice_router(SimpleNamespace(services={})))
    client = TestClient(app)
    assert client.post("/api/tts", json={"text": "hi"}).status_code == 503
    assert client.get("/api/voice/voices").status_code == 503
