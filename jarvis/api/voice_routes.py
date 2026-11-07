"""HTTP routes that let the HUD talk: transcribe, synthesise, pick voices, converse.

Mount with ``app.include_router(build_voice_router(kernel))``. The existing
``/api/voice/status``, ``/api/voice/say`` and ``/api/voice/listen`` stay in
``app.py``; nothing here duplicates them.

Routes
------
* ``POST /api/voice/transcribe`` — multipart ``file`` (webm/ogg/opus/wav...) →
  ``{ok, text, language, duration, reason}``. Decoded in memory / a private temp
  dir that is deleted immediately; the audio is never kept.
* ``POST /api/tts`` — ``{text, engine?, voice?, effect?, effect_strength?}`` →
  ``{ok, engine, duration_s, url, id, reason, ...}``; ``url`` serves the WAV from
  a bounded, TTL'd cache with unguessable ids. Plays on the server speaker only
  when ``voice.output`` is ``server`` or ``both``.
* ``GET /api/voice/audio/{id}`` — the cached WAV (404 once evicted/expired).
* ``GET /api/voice/voices`` / ``POST /api/voice/select`` — list and switch
  engine, voice, effect and strength at runtime.
* ``POST /api/voice/audition`` — speak a sample line with a given voice/effect
  without changing the active selection.
* ``POST /api/voice/converse`` — audio in → transcript → agent turn
  (``source="voice"``) → reply text + audio url.
* ``POST /api/voice/mic/stop`` — one-click stop for the server microphone loop.

Privacy: transcripts and reply text are never logged at INFO; uploads are
size/duration capped and content-type checked; synthesized audio expires.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..logging_setup import get_logger

log = get_logger("jarvis.api.voice")

__all__ = ["build_voice_router", "AudioCache", "SAMPLE_LINE"]

SAMPLE_LINE = ("Good evening, sir. All systems are online, and I have taken the "
               "liberty of warming up the coffee machine.")
MAX_TTS_CHARS = 4000
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")


class AudioCache:
    """Bounded, TTL'd map of unguessable id → WAV file.

    Files are only ever served by looking the id up in this map, so a request
    can never name a path (no traversal surface). Evicted/expired files are
    deleted from disk.
    """

    def __init__(self, max_items: int = 64, ttl_s: float = 15 * 60) -> None:
        self.max_items = max(1, int(max_items))
        self.ttl_s = float(ttl_s)
        self._items: OrderedDict[str, tuple[Path, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _expire_locked(self, now: float) -> list[Path]:
        dead: list[Path] = []
        for key in list(self._items):
            path, created = self._items[key]
            if now - created > self.ttl_s:
                dead.append(path)
                del self._items[key]
        while len(self._items) > self.max_items:
            _key, (path, _created) = self._items.popitem(last=False)
            dead.append(path)
        return dead

    @staticmethod
    def _delete(paths: list[Path]) -> None:
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def put(self, path: str | Path) -> str:
        key = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._items[key] = (Path(path), now)
            dead = self._expire_locked(now)
        self._delete(dead)
        return key

    def get(self, key: str) -> Path | None:
        if not isinstance(key, str) or not _ID_RE.match(key):
            return None
        now = time.time()
        with self._lock:
            dead = self._expire_locked(now)
            hit = self._items.get(key)
        self._delete(dead)
        if hit is None or not hit[0].is_file():
            return None
        return hit[0]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _voice_cfg(kernel: Any) -> Any:
    return getattr(getattr(kernel, "config", None), "voice", None)


def build_voice_router(kernel: Any, *, cache: AudioCache | None = None) -> APIRouter:
    router = APIRouter()
    audio_cache = cache or AudioCache()
    background: set[asyncio.Task[Any]] = set()
    warm_state = {"started": False, "result": None}

    def svc() -> Any:
        s = (getattr(kernel, "services", None) or {}).get("voice")
        if s is None:
            raise HTTPException(status_code=503, detail="voice service not loaded")
        return s

    def _keep(task: asyncio.Task[Any]) -> None:
        background.add(task)
        task.add_done_callback(background.discard)

    def _publish(topic: str, **data: Any) -> None:
        bus = getattr(kernel, "bus", None)
        if bus is not None:
            try:
                bus.publish(topic, **data)
            except Exception:  # pragma: no cover - observer must not break us
                pass

    def _warm_in_background(s: Any) -> None:
        """Load the TTS model once, off the request path."""
        if warm_state["started"]:
            return
        warm_state["started"] = True

        async def run() -> None:
            warm_state["result"] = await asyncio.to_thread(s.speaker.warm)
        _keep(asyncio.create_task(run()))

    def _active(s: Any) -> dict[str, Any]:
        sp = s.speaker
        v = s.voice_config
        return {
            "engine": sp.engine, "voice": sp.voice_name,
            "kokoro_voice": sp.kokoro_voice, "piper_voice": sp.piper_voice,
            "effect": sp.effect, "effect_strength": sp.effect_strength,
            "speed": sp.kokoro_speed, "chain": sp.chain,
            "output": s.output if hasattr(s, "output") else "browser",
            "volume": float(getattr(v, "volume", 0.65) or 0.65),
            "muted": bool(getattr(s, "muted", False)),
        }

    async def _synthesize_to_cache(s: Any, text: str, **overrides: Any) -> dict[str, Any]:
        result = await s.speaker.synthesize(text, **overrides)
        out = {k: result.get(k) for k in (
            "ok", "engine", "duration_s", "reason", "speech", "attempts",
            "fallback_reason", "voice", "effect", "effect_strength", "sample_rate")}
        out["url"] = None
        out["id"] = None
        path = result.get("path")
        if path and Path(path).is_file():
            key = audio_cache.put(path)
            out["id"] = key
            out["url"] = f"/api/voice/audio/{key}"
            out["_path"] = path
        return out

    def _maybe_play_on_server(s: Any, out: dict[str, Any], *, force_browser: bool) -> None:
        output = s.output if hasattr(s, "output") else "browser"
        path = out.pop("_path", None)
        if force_browser or output not in ("server", "both"):
            out["server_playback"] = f"skipped (output={output})"
            return
        if getattr(s, "muted", False):
            out["server_playback"] = "skipped (muted)"
            return
        if not (out.get("ok") and out.get("speech") and path):
            out["server_playback"] = "skipped (no speech audio)"
            return

        async def play() -> None:
            res = await asyncio.to_thread(s.player.play_file, path)
            if not res.get("ok"):
                log.warning("server playback failed: %s", res.get("reason"))
        _keep(asyncio.create_task(play()))
        out["server_playback"] = "started"

    # ------------------------------------------------------------- transcribe
    async def _transcribe_upload(s: Any, file: UploadFile) -> dict[str, Any]:
        from ..voice.decode import (MAX_UPLOAD_BYTES, TARGET_RATE, content_type_allowed,
                                    decode_audio_bytes)
        from ..voice.vad import trim_silence

        empty = {"ok": False, "text": "", "language": None, "duration": 0.0, "reason": ""}
        v = s.voice_config
        if not getattr(s, "enabled", True):
            return {**empty, "reason": "voice disabled (config.voice.enabled=false)"}
        if not bool(getattr(v, "stt_enabled", True)):
            return {**empty, "reason": "stt disabled (config.voice.stt_enabled=false)"}
        if not content_type_allowed(file.content_type or ""):
            raise HTTPException(status_code=415,
                                detail=f"unsupported content type '{file.content_type}'")
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        await file.close()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"audio upload exceeds {MAX_UPLOAD_BYTES} bytes")
        ok, reason = await s.transcriber.available()
        if not ok:
            return {**empty, "reason": reason}
        samples, reason = await asyncio.to_thread(
            decode_audio_bytes, data, file.filename or "", file.content_type or "")
        del data                                   # never kept
        if samples is None:
            return {**empty, "reason": reason}
        duration = round(len(samples) / TARGET_RATE, 3)
        trimmed, detected = trim_silence(samples, TARGET_RATE)
        if not detected:
            return {**empty, "duration": duration, "reason": "no speech detected (VAD gate)"}
        _publish("voice.stt.start", source="upload", seconds=duration)
        result = await s.transcriber.transcribe(trimmed, sample_rate=TARGET_RATE)
        text = (result.get("text") or "").strip()
        _publish("voice.stt.done", ok=bool(result.get("ok")), chars=len(text),
                 reason=result.get("reason", ""))
        return {"ok": bool(result.get("ok")) and bool(text), "text": text,
                "language": result.get("language"), "duration": duration,
                "reason": result.get("reason") or ("" if text else "no words recognised")}

    @router.post("/api/voice/transcribe")
    async def voice_transcribe(file: UploadFile = File(...)) -> dict[str, Any]:
        return await _transcribe_upload(svc(), file)

    # ------------------------------------------------------------- tts
    @router.post("/api/tts")
    async def tts(payload: dict[str, Any]) -> dict[str, Any]:
        s = svc()
        text = str((payload or {}).get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        if len(text) > MAX_TTS_CHARS:
            raise HTTPException(status_code=413, detail=f"text exceeds {MAX_TTS_CHARS} chars")
        v = s.voice_config
        if not getattr(s, "enabled", True) or not bool(getattr(v, "tts_enabled", True)):
            return {"ok": False, "engine": s.speaker.engine, "duration_s": 0.0, "url": None,
                    "id": None, "speech": False,
                    "reason": "tts disabled (voice.enabled/tts_enabled is false)"}
        overrides: dict[str, Any] = {}
        for key, arg in (("engine", "engine"), ("voice", "voice"), ("effect", "effect"),
                         ("effect_strength", "strength")):
            if (payload or {}).get(key) is not None:
                overrides[arg] = payload[key]
        out = await _synthesize_to_cache(s, text, **overrides)
        _maybe_play_on_server(s, out, force_browser=bool((payload or {}).get("browser_only")))
        _publish("voice.tts.done", ok=bool(out.get("ok") and out.get("speech")),
                 engine=out.get("engine"), duration_s=out.get("duration_s", 0.0),
                 played=out.get("server_playback") == "started", muted=bool(s.muted),
                 reason=out.get("reason", ""))
        return out

    @router.get("/api/voice/audio/{audio_id}")
    async def voice_audio(audio_id: str) -> FileResponse:
        path = audio_cache.get(audio_id)
        if path is None:
            raise HTTPException(status_code=404, detail="audio not found or expired")
        return FileResponse(str(path), media_type="audio/wav",
                            headers={"Cache-Control": "no-store",
                                     "X-Content-Type-Options": "nosniff"})

    # ------------------------------------------------------------- voices
    @router.get("/api/voice/voices")
    async def voices() -> dict[str, Any]:
        s = svc()
        v = s.voice_config
        engines = await asyncio.to_thread(s.speaker.list_voices)
        if getattr(s, "enabled", True) and bool(getattr(v, "tts_enabled", True)):
            _warm_in_background(s)
        return {
            "active": _active(s),
            "active_engine": await asyncio.to_thread(s.speaker.active_engine),
            "engines": engines,
            "effects": ["jarvis", "none"],
            "sample_line": SAMPLE_LINE,
            "conversation_timeout_s": float(getattr(v, "conversation_timeout_s", 8.0) or 8.0),
            "push_to_talk_hotkey": str(getattr(v, "push_to_talk_hotkey", "") or ""),
            "wake": {"enabled": bool(getattr(v, "wake_enabled", False)),
                     "model": getattr(getattr(s, "wake", None), "model_name", None),
                     "listening": bool(s.is_listening()) if hasattr(s, "is_listening") else False},
            "stt": {"model": getattr(s.transcriber, "model_name", None),
                    "device": getattr(s.transcriber, "device", None)},
            "warm": warm_state["result"],
        }

    @router.post("/api/voice/select")
    async def select(payload: dict[str, Any]) -> dict[str, Any]:
        s = svc()
        body = payload or {}
        try:
            applied = s.speaker.configure(
                engine=body.get("engine"), voice=body.get("voice"),
                effect=body.get("effect"), strength=body.get("effect_strength"),
                speed=body.get("speed"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        v = s.voice_config
        sp = s.speaker
        # Mirror into the live config so /api/settings and status agree
        # (runtime only; config/jarvis.toml is not rewritten).
        if v is not None:
            mirror = {"engine": ("tts_engine", sp.engine),
                      "effect": ("effect", sp.effect),
                      "effect_strength": ("effect_strength", sp.effect_strength),
                      "speed": ("kokoro_speed", sp.kokoro_speed)}
            for key, (attr, value) in mirror.items():
                if key in applied:
                    setattr(v, attr, value)
            if "voice" in applied:
                setattr(v, "kokoro_voice" if sp.engine == "kokoro" else "piper_voice",
                        applied["voice"])
        if "output" in body:
            out = str(body["output"]).strip().lower()
            if out not in ("browser", "server", "both"):
                raise HTTPException(status_code=400, detail="output must be browser|server|both")
            if v is not None:
                v.output = out
            applied["output"] = out
        if applied:
            audit = getattr(getattr(kernel, "db", None), "audit", None)
            if callable(audit):
                try:
                    audit("voice.select", actor="owner", detail=applied)
                except Exception:  # pragma: no cover - audit must not break selection
                    pass
            _publish("settings", changed={f"voice.{k}": val for k, val in applied.items()})
        return {"applied": applied, "active": _active(s),
                "active_engine": await asyncio.to_thread(sp.active_engine)}

    @router.post("/api/voice/audition")
    async def audition(payload: dict[str, Any]) -> dict[str, Any]:
        s = svc()
        body = payload or {}
        text = str(body.get("text") or SAMPLE_LINE).strip()[:400]
        overrides: dict[str, Any] = {}
        for key, arg in (("engine", "engine"), ("voice", "voice"), ("effect", "effect"),
                         ("effect_strength", "strength")):
            if body.get(key) is not None:
                overrides[arg] = body[key]
        out = await _synthesize_to_cache(s, text, **overrides)
        out.pop("_path", None)                     # auditions play in the browser only
        out["text"] = text
        return out

    # ------------------------------------------------------------- converse
    @router.post("/api/voice/converse")
    async def converse(file: UploadFile = File(...),
                       session_id: str = Form("default")) -> dict[str, Any]:
        s = svc()
        heard = await _transcribe_upload(s, file)
        out: dict[str, Any] = {"ok": False, "transcript": heard.get("text", ""),
                               "stt": heard, "reply": "", "audio": None, "reason": ""}
        if not heard.get("ok"):
            out["reason"] = heard.get("reason") or "nothing heard"
            return out
        agent = getattr(kernel, "agent", None)
        if agent is None:
            out["reason"] = "agent not available"
            return out
        session = kernel.session(session_id or "default") if hasattr(kernel, "session") else {}
        reply = await agent.complete(heard["text"], session=session, source="voice")
        out["reply"] = reply or ""
        if not reply:
            out["reason"] = "agent produced no reply"
            return out
        audio = await _synthesize_to_cache(s, reply)
        _maybe_play_on_server(s, audio, force_browser=False)
        out["audio"] = audio
        out["ok"] = bool(audio.get("ok") and audio.get("speech"))
        out["reason"] = "" if out["ok"] else (audio.get("reason") or "speech synthesis failed")
        return out

    # ------------------------------------------------------------- mic control
    @router.post("/api/voice/mic/stop")
    async def mic_stop() -> dict[str, Any]:
        s = svc()
        was = bool(s.running) if hasattr(s, "running") else False
        if hasattr(s, "stop"):
            await s.stop()
        return {"ok": True, "was_running": was, "running": bool(getattr(s, "running", False))}

    return router
