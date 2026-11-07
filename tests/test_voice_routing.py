"""Voice routing: node voice.play handler and the core voice.speak tool.

Covers the new ``voice.play`` node task kind (decode/validate/play) and the
``voice.speak`` core tool that synthesises once and broadcasts the same WAV to
chosen hosts. Hardware-independent: playback is mocked, no real audio device.
"""

from __future__ import annotations

import base64
import wave
from pathlib import Path

import pytest

from jarvis.nodes import protocol
from jarvis.nodes.agent import NodeAgent
from jarvis.tools import REGISTRY
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import hosts  # noqa: F401  (registers voice.speak + host.*)


def _make_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 2400)  # 0.1 s of silence
    return path


# ---------------------------------------------------------------- protocol ---

def test_voice_play_is_a_supported_kind():
    assert protocol.KIND_VOICE_PLAY == "voice.play"
    assert protocol.KIND_VOICE_PLAY in protocol.SUPPORTED_KINDS


# ---------------------------------------------------------------- node agent ---

@pytest.fixture
def agent():
    return NodeAgent("secret")


def test_voice_play_requires_audio(agent):
    r = agent._task_voice_play({}, "t1")
    assert not r["ok"] and "audio_b64" in r["error"]


def test_voice_play_rejects_bad_base64(agent):
    r = agent._task_voice_play({"audio_b64": "!!not-base64!!"}, "t1")
    assert not r["ok"] and "base64" in r["error"]


def test_voice_play_rejects_oversized_audio(agent):
    big = base64.b64encode(b"x" * (6_000_000)).decode("ascii")
    r = agent._task_voice_play({"audio_b64": big}, "t1")
    assert not r["ok"] and "too large" in r["error"]


def test_voice_play_writes_and_plays(agent, monkeypatch):
    played = {}

    def fake_play(path, timeout_s):
        assert Path(path).suffix == ".wav"
        assert Path(path).read_bytes().startswith(b"RIFF")
        played["path"] = path
        return {"ok": True, "engine": "test-player"}

    monkeypatch.setattr(agent, "_play_wav", fake_play)
    payload = {"audio_b64": base64.b64encode(b"RIFFxxxxWAVE").decode("ascii"),
               "sample_rate": 24000}
    r = agent._task_voice_play(payload, "t1")
    assert r["ok"] is True
    assert r["data"]["played"] is True and r["data"]["engine"] == "test-player"
    assert not Path(played["path"]).exists()  # temp file was cleaned up


def test_voice_play_reports_playback_failure(agent, monkeypatch):
    monkeypatch.setattr(agent, "_play_wav",
                        lambda path, timeout_s: {"ok": False, "reason": "no audio device"})
    r = agent._task_voice_play(
        {"audio_b64": base64.b64encode(b"RIFFxxxxWAVE").decode("ascii")}, "t1")
    assert not r["ok"] and "no audio device" in r["error"]


# ---------------------------------------------------------------- core tool ---

class _FakeSpeaker:
    def __init__(self, wav: Path):
        self._wav = wav

    async def synthesize(self, text, **kw):
        return {"ok": True, "path": str(self._wav), "engine": "kokoro",
                "sample_rate": 24000, "duration_s": 0.1}


class _FakePlayer:
    def __init__(self):
        self.played = []

    def play_file(self, path):
        self.played.append(path)
        return {"ok": True}


class _FakeVoice:
    enabled = True

    def __init__(self, wav: Path):
        self.speaker = _FakeSpeaker(wav)
        self.player = _FakePlayer()


class _FakeNodes:
    def __init__(self, known):
        self.known = known
        self.calls = []

    async def send_task(self, node_id, kind, payload):
        self.calls.append((node_id, kind, payload))
        return {"ok": True, "data": {"played": True}}


@pytest.fixture
def voice_ctx(cfg, tmp_path):
    wav = _make_wav(tmp_path / "voice.wav")
    voice = _FakeVoice(wav)
    nodes = _FakeNodes({"pc": "http://x:2228", "laptop": "http://x:2229"})
    cfg.nodes.known = {"pc": "http://x:2228", "laptop": "http://x:2229"}
    cfg.voice.default_hosts = ["pc"]
    ctx = ToolContext(config=cfg, db=None, bus=None,
                      services={"voice": voice, "nodes": nodes})
    return ctx, voice, nodes


async def test_voice_speak_routes_to_named_hosts(voice_ctx):
    ctx, voice, nodes = voice_ctx
    r = await REGISTRY.call("voice.speak", ctx, text="Hello", hosts="pc,laptop")
    assert r.ok, r.error
    assert set(r.data["spoken"]) == {"pc", "laptop"}
    assert not r.data["errors"]
    assert [c[0] for c in nodes.calls] == ["pc", "laptop"]
    assert all(c[1] == "voice.play" for c in nodes.calls)
    assert all("audio_b64" in c[2] and c[2]["audio_b64"] for c in nodes.calls)


async def test_voice_speak_all_includes_local(voice_ctx):
    ctx, voice, nodes = voice_ctx
    r = await REGISTRY.call("voice.speak", ctx, text="Hello", hosts="all")
    assert r.ok
    assert set(r.data["spoken"]) == {"pc", "laptop", "apos"}
    assert [c[0] for c in nodes.calls] == ["pc", "laptop"]
    assert voice.player.played  # local (apos) playback was attempted


async def test_voice_speak_alias_resolution(voice_ctx):
    ctx, voice, nodes = voice_ctx
    r = await REGISTRY.call("voice.speak", ctx, text="Hello", hosts="pc-001")
    assert r.ok and list(r.data["spoken"]) == ["pc"]


async def test_voice_speak_defaults_to_config(voice_ctx):
    ctx, voice, nodes = voice_ctx
    r = await REGISTRY.call("voice.speak", ctx, text="Hello")
    assert r.ok and list(r.data["spoken"]) == ["pc"]


async def test_voice_speak_reports_unreachable_host(voice_ctx):
    ctx, voice, nodes = voice_ctx

    async def failing(node_id, kind, payload):
        raise RuntimeError("unreachable")

    nodes.send_task = failing
    r = await REGISTRY.call("voice.speak", ctx, text="Hello", hosts="laptop")
    assert not r.ok
    assert "laptop" in r.data["errors"]
    assert not r.data["spoken"]


async def test_voice_speak_without_voice_service(cfg):
    ctx = ToolContext(config=cfg, db=None, bus=None, services={})
    r = await REGISTRY.call("voice.speak", ctx, text="Hello")
    assert not r.ok and "voice service" in r.error
