"""PC-001 console-free wake-word voice client.

Listens for "hey jarvis" (openwakeword), records the utterance, transcribes it
locally (faster-whisper), sends the transcript to the Core over HTTP, then
synthesises the reply with the Core's Kokoro voice and plays it on the local
speakers.

Runs under ``pythonw.exe`` (no console). The Core is reached at
``JARVIS_CORE_URL`` (default ``http://127.0.0.1:8765`` — the SSH tunnel to APOS).

Honest degradation: if the wake word, STT, microphone or Core is unavailable the
loop logs exactly why and keeps retrying rather than silently doing nothing.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any

# Make the repo importable when launched directly (sys.path[0] is scripts/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from jarvis.config import load_config
from jarvis.logging_setup import get_logger, setup_logging
from jarvis.voice import audio
from jarvis.voice.stt import Transcriber
from jarvis.voice.vad import VadGate
from jarvis.voice.wake import WakeWord

log = get_logger("jarvis.voice.listener")

CORE_URL = os.environ.get("JARVIS_CORE_URL", "http://127.0.0.1:8765").rstrip("/")


def _empty(reason: str) -> dict[str, Any]:
    return {"ok": False, "text": "", "reason": reason}


class VoiceListener:
    """Wake-word client that drives the remote Core, not a local agent."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.wake = WakeWord(config)
        self.transcriber = Transcriber(config)
        self.player = audio.Player(config)
        v = getattr(config, "voice", config)
        self.sample_rate = int(getattr(v, "sample_rate", 16000) or 16000)
        self.max_utterance_s = 15.0
        self.conversation_timeout_s = max(
            1.0, float(getattr(v, "conversation_timeout_s", 8.0) or 8.0))
        self._client: httpx.AsyncClient | None = None

    # -- Core HTTP -------------------------------------------------------
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=180.0)
        return self._client

    async def _core_chat(self, text: str) -> str:
        r = await self.client().post(f"{CORE_URL}/api/chat/sync",
                                     json={"message": text, "source": "voice"})
        r.raise_for_status()
        return (r.json() or {}).get("reply", "")

    async def _core_tts(self, text: str) -> dict[str, Any]:
        r = await self.client().post(f"{CORE_URL}/api/tts",
                                     json={"text": text, "browser_only": True})
        r.raise_for_status()
        return r.json()

    async def _play_wav_bytes(self, data: bytes) -> dict[str, Any]:
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="jarvis-listener-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return await asyncio.to_thread(self.player.play_file, path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def _speak(self, text: str) -> bool:
        """Synthesise via the Core and play locally; returns whether audio played."""
        try:
            tts = await self._core_tts(text)
        except Exception as exc:
            log.warning("core TTS failed: %s", exc)
            return False
        url = tts.get("url") if isinstance(tts, dict) else None
        if not (tts.get("ok") and url):
            return False
        try:
            r = await self.client().get(f"{CORE_URL}{url}")
            r.raise_for_status()
            data = r.content
        except Exception as exc:
            log.warning("core audio download failed: %s", exc)
            return False
        played = await self._play_wav_bytes(data)
        return bool(played.get("ok"))

    async def _handle_utterance(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        t0 = time.perf_counter()
        try:
            reply = await self._core_chat(text)
        except Exception as exc:
            log.warning("core chat failed: %s", exc)
            return
        t1 = time.perf_counter()
        reply = (reply or "").strip()
        if reply:
            log.info("speaking reply (%d chars)", len(reply))
            await self._speak(reply)
        log.info("timing: core-chat=%.2fs speak+tts+play=%.2fs",
                 t1 - t0, time.perf_counter() - t1)

    # -- capture ---------------------------------------------------------
    async def _capture_utterance(self, mic: Any, wait_s: float,
                                 pre_roll: list[Any] | None = None) -> dict[str, Any]:
        import numpy as np
        v = self.config.voice
        threshold = float(getattr(v, "vad_threshold", 0.02) or 0.02)
        gate = VadGate(sample_rate=self.sample_rate, threshold=threshold)
        frame_s = max(1e-3, float(getattr(mic, "frame_ms", 30)) / 1000.0)
        preroll: list[Any] = []
        frames: list[Any] = []
        waited = 0.0
        speaking = False
        spoken_s = 0.0
        # Seed with the wake pre-roll so a command spoken right after "jarvis"
        # is kept; the leading wake word is stripped after transcription.
        if pre_roll:
            for f in pre_roll:
                frames.append(f)
                gate.feed(f)
            speaking = gate.in_speech
            spoken_s = frame_s * len(frames)
        while True:
            frame = await asyncio.to_thread(mic.get_frame, 0.5)
            if frame is None:
                waited += 0.5
                if speaking and waited >= 0.8:
                    break
                if not speaking and waited >= wait_s:
                    log.info("capture: no speech before timeout (%.1fs)", wait_s)
                    return _empty("no speech before timeout")
                continue
            state = gate.feed(frame)
            if not speaking:
                waited += frame_s
                preroll.append(frame)
                if len(preroll) > 10:
                    preroll.pop(0)
                if state == "start":
                    speaking = True
                    frames.extend(preroll)
                elif waited >= wait_s:
                    log.info("capture: no speech before timeout (%.1fs)", wait_s)
                    return _empty("no speech before timeout")
                continue
            frames.append(frame)
            spoken_s += frame_s
            if state == "end" or spoken_s >= self.max_utterance_s:
                break
        if not frames:
            return _empty("stopped")
        data = np.concatenate([np.asarray(f, dtype=np.float32).reshape(-1) for f in frames])
        t_stt = time.perf_counter()
        result = await self.transcriber.transcribe(data, sample_rate=self.sample_rate)
        log.info("capture: heard %r (%.2fs) [stt=%.2fs]",
                 result.get("text", ""), spoken_s, time.perf_counter() - t_stt)
        return result

    def _drain(self, mic: Any) -> None:
        dropped = 0
        while mic.get_frame(0.0) is not None:
            dropped += 1
            if dropped > 10000:
                break

    async def _conversation(self, mic: Any, pre_roll: list[Any] | None = None) -> None:
        """After the wake word: first utterance, then follow-ups until silence."""
        t_start = time.perf_counter()
        captured = await self._capture_utterance(mic, 5.0, pre_roll=pre_roll)
        t_captured = time.perf_counter()
        text = re.sub(r"^(?:(?:hey|ok|okay|yo)\s+)?jarvis[\s,!.?]*", "",
                      (captured.get("text") or ""), flags=re.IGNORECASE).strip()
        if not text:
            log.info("timing: wake->capture=%.2fs (no speech)", t_captured - t_start)
            return
        log.info("heard: %r", text)
        await self._handle_utterance(text)
        log.info("timing: wake->capture=%.2fs capture->reply-done=%.2fs TOTAL=%.2fs",
                 t_captured - t_start, time.perf_counter() - t_captured,
                 time.perf_counter() - t_start)
        self._drain(mic)  # never transcribe our own reply
        # follow-ups without re-waking, until a silent timeout
        while True:
            follow = await self._capture_utterance(mic, self.conversation_timeout_s)
            text = re.sub(r"^(?:(?:hey|ok|okay|yo)\s+)?jarvis[\s,!.?]*", "",
                          (follow.get("text") or ""), flags=re.IGNORECASE).strip()
            if not text:
                return
            log.info("follow-up: %r", text)
            await self._handle_utterance(text)
            self._drain(mic)

    # -- main loop -------------------------------------------------------
    async def run_forever(self) -> None:
        # Probe prerequisites once; report each and keep going when transient.
        wake_ok, wake_reason = await self.wake.available()
        stt_ok, stt_reason = await self.transcriber.available()
        out_ok, out_reason = audio.probe_output(self.config)
        log.info("listener prerequisites: wake=%s (%s), stt=%s (%s), output=%s (%s)",
                 wake_ok, wake_reason, stt_ok, stt_reason, out_ok, out_reason)
        if not (wake_ok and stt_ok and out_ok):
            log.error("voice listener cannot start: wake=%s stt=%s output=%s",
                      wake_ok, stt_ok, out_ok)

        while True:
            mic = audio.MicStream(self.config)
            ok, reason = mic.start()
            if not ok:
                log.warning("mic unavailable (%s); retrying in 10s", reason)
                await asyncio.sleep(10.0)
                continue
            log.info("wake loop listening (wake=%s, model=%s)",
                     self.wake.model_name, self.transcriber.model_name)
            pre_roll: deque[Any] = deque(maxlen=80)  # ~2.4s of 30ms frames
            try:
                while True:
                    frame = await asyncio.to_thread(mic.get_frame, 1.0)
                    if frame is None:
                        continue
                    pre_roll.append(frame)
                    if not self.wake.feed(frame):
                        continue
                    log.info("wake word detected")
                    self.wake.reset()
                    await self._conversation(mic, list(pre_roll))
                    self.wake.reset()
                    pre_roll.clear()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - loop must survive
                log.error("listener loop error: %s", exc)
            finally:
                try:
                    mic.stop()
                except Exception:
                    pass
            await asyncio.sleep(2.0)  # brief pause before reopening the mic


def main() -> int:
    config = load_config()
    config.voice.enabled = True
    setup_logging(config.log_level, log_dir=config.data_path / "logs")
    listener = VoiceListener(config)
    try:
        asyncio.run(listener.run_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
