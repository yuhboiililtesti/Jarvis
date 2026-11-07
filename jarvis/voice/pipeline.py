"""The voice service the kernel and UI talk to.

``VoiceService`` wires together capture (``audio``), transcription (``stt``),
synthesis (``tts`` engine chain + ``effects``), voice-activity detection
(``vad``) and wake-word detection (``wake``). Every capability reports itself
*available with a reason*, and the service never claims to be listening when it
is not: ``start()`` only spins up the microphone loop when the wake word is
enabled *and* every prerequisite probes available, otherwise it records exactly
why it stayed stopped.

Server-side conversation (wake word path): "hey jarvis" → capture one utterance
(VAD end-pointed) → agent turn with ``source="voice"`` → speak the reply sentence
by sentence on the server speaker → listen for a follow-up for
``voice.conversation_timeout_s`` without needing the wake word again.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from . import audio
from .stt import Transcriber
from .text import clean_for_speech, split_sentences
from .tts import Speaker, _temp_wav, write_wav
from .vad import VadGate, trim_silence
from .wake import WakeWord

log = get_logger("jarvis.voice")

__all__ = ["VoiceService", "VoiceStatus"]


@dataclass
class VoiceStatus:
    """Availability of a single voice capability."""

    available: bool
    reason: str = ""
    detail: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"available": bool(self.available), "reason": self.reason,
                "detail": self.detail}


def _voice(config: Any) -> Any:
    return getattr(config, "voice", config) if config is not None else None


def _join(*parts: str) -> str:
    return "; ".join(p for p in parts if p)


def _strip_wake_word(text: str) -> str:
    """Remove a leading wake word ("jarvis", "hey jarvis", "ok jarvis", ...).

    The pre-roll buffer that preserves a command spoken immediately after the
    wake word also carries the wake word itself; drop it so the agent hears the
    command, not "jarvis".
    """
    return re.sub(r"^(?:(?:hey|ok|okay|yo)\s+)?jarvis[\s,!.?]*", "",
                  text, flags=re.IGNORECASE).strip()


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class VoiceService:
    """Owns the voice stack and exposes it to the rest of JARVIS."""

    def __init__(self, jarvis: Any) -> None:
        self.jarvis = jarvis
        self.config = getattr(jarvis, "config", None)
        self.bus = getattr(jarvis, "bus", None)
        self.agent = getattr(jarvis, "agent", None)
        v = _voice(self.config)
        self.voice_config = v
        self.enabled = bool(getattr(v, "enabled", True))
        self.muted = False

        self.transcriber = Transcriber(self.config)
        self.speaker = Speaker(self.config)
        self.wake = WakeWord(self.config)
        self.player = audio.Player(self.config)
        self.sample_rate = int(getattr(v, "sample_rate", 16000) or 16000)
        self.gate = VadGate(sample_rate=self.sample_rate)

        self.mic: audio.MicStream | None = None
        self.listen_seconds = 6.0
        self.conversation_timeout_s = max(
            1.0, _float(getattr(v, "conversation_timeout_s", 8.0), 8.0))
        self.max_utterance_s = 15.0
        self._task: asyncio.Task[Any] | None = None
        self._stopping = False
        self._state = "stopped"
        self._state_reason = "not started"

    # -- helpers ---------------------------------------------------------
    def _publish(self, topic: str, **data: Any) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(topic, **data)
        except Exception as exc:  # pragma: no cover - observer must not break us
            log.debug("bus publish failed: %s", exc)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_listening(self) -> bool:
        """Honest listening state: True only while the mic loop is live."""
        return self.running and not self.muted

    def set_muted(self, muted: bool) -> bool:
        """Mute/unmute output and wake handling; publishes a visible state."""
        self.muted = bool(muted)
        self._publish("voice.muted", muted=self.muted)
        self._publish("status", component="voice", state="muted" if self.muted else "unmuted")
        return self.muted

    @property
    def output(self) -> str:
        out = str(getattr(self.voice_config, "output", "browser") or "browser").lower()
        return out if out in ("browser", "server", "both") else "browser"

    # -- status ----------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        """One dict describing every capability and the current state."""
        stt_ok, stt_reason = await self.transcriber.available()
        tts_ok, tts_reason = await self.speaker.available()
        wake_ok, wake_reason = await self.wake.available()
        in_ok, in_reason = audio.probe_input(self.config)
        out_ok, out_reason = audio.probe_output(self.config)
        sp = self.speaker

        stt = VoiceStatus(stt_ok, stt_reason,
                          {"model": self.transcriber.model_name,
                           "device": self.transcriber.device,
                           "compute_type": self.transcriber.compute_type})
        tts = VoiceStatus(tts_ok, tts_reason,
                          {"engine": sp.engine,
                           "active_engine": await asyncio.to_thread(sp.active_engine),
                           "chain": sp.chain,
                           "voice": sp.voice_name,
                           "effect": sp.effect,
                           "effect_strength": sp.effect_strength,
                           "backend": sp.backend_name(),
                           "voices_dir": str(sp.voices_dir),
                           "kokoro_dir": str(sp.kokoro_dir),
                           "output": self.output})
        wake = VoiceStatus(wake_ok, wake_reason,
                           {"model": self.wake.model_name,
                            "threshold": self.wake.threshold})
        inp = VoiceStatus(in_ok, in_reason, {"devices": list(audio.list_input_devices())})
        out = VoiceStatus(out_ok, out_reason, {"devices": list(audio.list_output_devices())})

        v = self.voice_config
        return {
            "enabled": self.enabled,
            "stt_enabled": bool(getattr(v, "stt_enabled", True)),
            "tts_enabled": bool(getattr(v, "tts_enabled", True)),
            "wake_enabled": bool(getattr(v, "wake_enabled", False)),
            "muted": self.muted,
            "running": self.running,
            "listening": self.is_listening(),
            "state": self._state,
            "state_reason": self._state_reason,
            "engine": sp.engine,
            "output_mode": self.output,          # browser | server | both
            "conversation_timeout_s": self.conversation_timeout_s,
            "stt": stt.to_dict(),
            "tts": tts.to_dict(),
            "wake": wake.to_dict(),
            "input": inp.to_dict(),
            "output": out.to_dict(),             # speaker device (unchanged key)
        }

    # -- speech output ---------------------------------------------------
    async def say(self, text: str, *, play: bool = True) -> dict[str, Any]:
        """Synthesise ``text`` sentence by sentence and play it on the server.

        Synthesis of sentence *n+1* overlaps playback of sentence *n*, so the
        first audio starts as soon as the first sentence is ready. The parts
        are also joined into one WAV (``path``) for callers that want a file.
        """
        text = (text or "").strip()
        result: dict[str, Any] = {
            "ok": False, "path": None, "engine": self.speaker.engine,
            "duration_s": 0.0, "speech": False, "played": False, "reason": "",
            "parts": [],
        }
        v = self.voice_config
        if not self.enabled:
            result["reason"] = "voice disabled (config.voice.enabled=false)"
            return result
        if not bool(getattr(v, "tts_enabled", True)):
            result["reason"] = "tts disabled (config.voice.tts_enabled=false)"
            return result
        sentences = split_sentences(clean_for_speech(text))
        if not sentences:
            result["reason"] = "empty text"
            return result

        self._publish("voice.tts.start", chars=len(text), engine=self.speaker.engine,
                      sentences=len(sentences))
        started = time.perf_counter()
        will_play = play and not self.muted
        parts: list[dict[str, Any]] = []
        reasons: list[str] = []
        played_all = will_play
        first_audio_s: float | None = None

        pending = asyncio.create_task(self.speaker.synthesize(sentences[0]))
        for i in range(len(sentences)):
            synth = await pending
            if i + 1 < len(sentences):
                pending = asyncio.create_task(self.speaker.synthesize(sentences[i + 1]))
            parts.append(synth)
            if synth.get("reason"):
                reasons.append(synth["reason"])
            if not (synth.get("ok") and synth.get("path")):
                played_all = False
                continue
            if first_audio_s is None:
                first_audio_s = time.perf_counter() - started
            if will_play:
                played = await asyncio.to_thread(self.player.play_file, synth["path"])
                if not played.get("ok"):
                    played_all = False
                    reasons.append(played.get("reason", "playback failed"))

        good = [p for p in parts if p.get("ok") and p.get("speech")]
        result["parts"] = [{k: p.get(k) for k in ("ok", "engine", "duration_s",
                                                    "fallback_reason", "reason")}
                           for p in parts]
        if good:
            joined, seconds = await asyncio.to_thread(self._join_parts, good)
            if joined is not None and len(good) > 1:
                # Parts are temporary; the joined file is the one artifact kept
                # (and it is swept by the TTL in tts.sweep_temp).
                for p in parts:
                    try:
                        if p.get("path"):
                            Path(p["path"]).unlink(missing_ok=True)
                    except OSError:
                        pass
            result.update({
                "ok": len(good) == len(parts), "speech": True,
                "path": str(joined) if joined else good[0]["path"],
                "engine": good[0]["engine"],
                "duration_s": round(seconds, 3),
                "fallback_reason": _join(*{p.get("fallback_reason", "") for p in good}),
                "voice": good[0].get("voice"), "effect": good[0].get("effect"),
            })
        else:
            last = parts[-1] if parts else {}
            result.update({"engine": last.get("engine", self.speaker.engine),
                           "path": last.get("path")})
        result["played"] = bool(will_play and played_all and good)
        if self.muted and play:
            reasons.append("muted; not played")
        elif not play:
            reasons.append("playback not requested")
        result["reason"] = _join(*dict.fromkeys(reasons))
        result["first_audio_s"] = round(first_audio_s, 3) if first_audio_s is not None else None
        result["elapsed_s"] = round(time.perf_counter() - started, 3)
        self._publish("voice.tts.done", ok=bool(result.get("ok") and result.get("speech")),
                      engine=result["engine"], duration_s=result.get("duration_s", 0.0),
                      played=result.get("played", False), muted=self.muted,
                      reason=result.get("reason", ""))
        return result

    @staticmethod
    def _join_parts(parts: list[dict[str, Any]]) -> tuple[Path | None, float]:
        """Concatenate same-rate part WAVs (with a short gap) into one file."""
        import numpy as np
        arrays: list[Any] = []
        rate = None
        for p in parts:
            try:
                data, sr = audio.read_wav(p["path"])
            except Exception:
                return None, sum(float(x.get("duration_s") or 0.0) for x in parts)
            if rate is None:
                rate = sr
            if sr != rate:  # mixed engines: do not resample here, keep parts
                return None, sum(float(x.get("duration_s") or 0.0) for x in parts)
            arr = np.asarray(data, dtype=np.float32)
            if arr.ndim > 1:
                arr = arr.mean(axis=1)
            if arrays:
                arrays.append(np.zeros(int(0.12 * sr), dtype=np.float32))
            arrays.append(arr)
        if len(parts) == 1:
            return Path(parts[0]["path"]), float(parts[0].get("duration_s") or 0.0)
        target = _temp_wav()
        seconds = write_wav(target, np.concatenate(arrays), int(rate or 16000))
        return target, seconds

    # -- speech input ----------------------------------------------------
    @staticmethod
    def _empty_listen(reason: str) -> dict[str, Any]:
        return {"ok": False, "text": "", "language": None, "duration": 0.0,
                "segments": [], "speech": False, "reason": reason}

    async def listen_once(self, seconds: float = 6.0) -> dict[str, Any]:
        """Record, trim silence, transcribe. Never blocks the event loop."""
        v = self.voice_config
        if not self.enabled:
            return self._empty_listen("voice disabled (config.voice.enabled=false)")
        if not bool(getattr(v, "stt_enabled", True)):
            return self._empty_listen("stt disabled (config.voice.stt_enabled=false)")
        ok, reason = await self.transcriber.available()
        if not ok:
            return self._empty_listen(reason)

        self._publish("voice.stt.start", seconds=seconds)
        rec = await asyncio.to_thread(audio.record, seconds, config=self.config)
        if not rec.get("ok"):
            return self._empty_listen(rec.get("reason", "capture failed"))

        data = rec["data"]
        rate = int(rec.get("samplerate") or getattr(v, "sample_rate", 16000))
        trimmed, detected = trim_silence(data, rate)
        if not detected:
            self._publish("voice.stt.done", ok=False, chars=0, reason="no speech (VAD)")
            return self._empty_listen("no speech detected (VAD gate)")

        result = await self.transcriber.transcribe(trimmed, sample_rate=rate)
        result["speech"] = bool(detected)
        self._publish("voice.stt.done", ok=bool(result.get("ok")),
                      chars=len(result.get("text", "")), reason=result.get("reason", ""))
        return result

    async def handle_utterance(self, text: str) -> str:
        """Push transcribed text through the agent (spoken mode) and speak the reply."""
        text = (text or "").strip()
        if not text:
            return ""
        if self.agent is None:
            log.warning("no agent available; cannot handle utterance")
            return ""
        session = self.jarvis.session() if hasattr(self.jarvis, "session") else {}
        self._publish("voice.state", state="thinking")
        reply = await self.agent.complete(text, session=session, source="voice")
        if reply:
            self._publish("voice.state", state="speaking")
            await self.say(reply)
        return reply

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> dict[str, Any]:
        """Start the background mic loop only when it can honestly listen."""
        if self.running:
            return {"running": True, "reason": "already listening"}
        v = self.voice_config
        # Warm the TTS engine in the background so the first spoken reply is not
        # cold (Kokoro model load is several seconds). Runs even when the mic
        # loop stays off — the headless core still serves /api/tts.
        if self.enabled and bool(getattr(v, "tts_enabled", True)):
            async def _warm_tts() -> None:
                try:
                    await asyncio.to_thread(self.speaker.warm)
                    log.info("tts warmed: %s", self.speaker.active_engine())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive
                    log.debug("tts warm failed: %s", exc)
            self._warm_tts_task = asyncio.create_task(_warm_tts())
        reasons: list[str] = []
        if not self.enabled:
            reasons.append("voice disabled (config.voice.enabled=false)")
        if not bool(getattr(v, "wake_enabled", False)):
            reasons.append("wake word disabled (config.voice.wake_enabled=false)")
        if not bool(getattr(v, "stt_enabled", True)):
            reasons.append("stt disabled (config.voice.stt_enabled=false)")

        # Cheap config gates first: never import/probe heavy stacks when off.
        if not reasons:
            wake_ok, wake_reason = await self.wake.available()
            if not wake_ok:
                reasons.append(f"wake: {wake_reason}")
            stt_ok, stt_reason = await self.transcriber.available()
            if not stt_ok:
                reasons.append(f"stt: {stt_reason}")
            in_ok, in_reason = audio.probe_input(self.config)
            if not in_ok:
                reasons.append(f"input: {in_reason}")

        if reasons:
            self._state = "stopped"
            self._state_reason = _join(*reasons)
            log.info("voice mic loop NOT started: %s", self._state_reason)
            self._publish("voice.status", state="stopped", reason=self._state_reason)
            return {"running": False, "reason": self._state_reason}

        try:
            self.mic = audio.MicStream(self.config)
            ok, reason = self.mic.start()
            if not ok:
                self.mic = None
                self._state = "stopped"
                self._state_reason = reason
                log.warning("voice mic loop NOT started: %s", reason)
                self._publish("voice.status", state="stopped", reason=reason)
                return {"running": False, "reason": reason}
        except Exception as exc:
            self.mic = None
            self._state = "stopped"
            self._state_reason = str(exc)
            log.warning("voice mic loop NOT started: %s", exc)
            self._publish("voice.status", state="stopped", reason=str(exc))
            return {"running": False, "reason": str(exc)}

        self._stopping = False
        try:
            self._task = asyncio.create_task(self._listen_loop(), name="voice-mic-loop")
        except Exception as exc:
            log.warning("could not start wake loop task: %s", exc)
            self._state = "stopped"
            self._state_reason = str(exc)
            self._publish("voice.status", state="stopped", reason=str(exc))
            return {"running": False, "reason": str(exc)}
        self._state = "listening"
        self._state_reason = ""
        self._publish("voice.status", state="listening", wake=self.wake.model_name)
        return {"running": True, "reason": ""}

    async def stop(self) -> None:
        """Stop the mic loop and release the input stream."""
        self._stopping = True
        warm = getattr(self, "_warm_tts_task", None)
        if warm is not None and not warm.done():
            warm.cancel()
        if self.mic is not None:
            try:
                self.mic.stop()
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("mic stop failed: %s", exc)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("mic loop ended with error: %s", exc)
            self._task = None
        self.mic = None
        self._state = "stopped"
        self._state_reason = "stopped by request"
        self._publish("voice.status", state="stopped")

    def _drain_mic(self) -> int:
        """Drop queued frames (e.g. our own voice captured during playback)."""
        dropped = 0
        while self.mic is not None and self.mic.get_frame(0.0) is not None:
            dropped += 1
            if dropped > 10000:  # pragma: no cover - defensive
                break
        return dropped

    async def capture_utterance(self, wait_s: float,
                                pre_roll: list[Any] | None = None) -> dict[str, Any]:
        """Capture one VAD-end-pointed utterance from the open mic stream.

        Waits up to ``wait_s`` for speech to start, then records until the VAD
        gate reports end-of-speech or ``max_utterance_s`` elapses, and
        transcribes it. ``pre_roll`` (frames buffered around the wake word) is
        prepended so a command spoken immediately after "jarvis" is not lost; the
        wake word itself is stripped by the caller. Returns the transcriber
        result (``ok=False`` + reason when nothing was said).
        """
        import numpy as np
        if self.mic is None:
            return self._empty_listen("microphone stream not open")
        v = self.voice_config
        threshold = float(getattr(v, "vad_threshold", 0.02) or 0.02)
        gate = VadGate(sample_rate=self.sample_rate, threshold=threshold)
        frame_s = max(1e-3, float(getattr(self.mic, "frame_ms", 30)) / 1000.0)
        preroll: list[Any] = []
        frames: list[Any] = []
        waited = 0.0
        speaking = False
        spoken_s = 0.0
        # Seed the capture with the wake pre-roll and advance the VAD state past
        # the wake word, so an utterance already in the buffer is preserved.
        if pre_roll:
            for f in pre_roll:
                frames.append(f)
                gate.feed(f)
            speaking = gate.in_speech
            spoken_s = frame_s * len(frames)
        self._publish("voice.state", state="listening")
        while not self._stopping:
            frame = await asyncio.to_thread(self.mic.get_frame, 0.5)
            if frame is None:
                waited += 0.5
                if speaking and waited >= 0.8:   # short tail once already speaking
                    break
                if not speaking and waited >= wait_s:
                    log.info("capture_utterance: no speech before timeout (%.1fs)", wait_s)
                    return self._empty_listen("no speech before timeout")
                continue
            state = gate.feed(frame)
            if not speaking:
                waited += frame_s
                preroll.append(frame)
                if len(preroll) > 10:          # ~300 ms pre-roll
                    preroll.pop(0)
                if state == "start":
                    speaking = True
                    frames.extend(preroll)
                    self._publish("voice.stt.start", source="mic")
                elif waited >= wait_s:
                    log.info("capture_utterance: no speech before timeout (%.1fs)", wait_s)
                    return self._empty_listen("no speech before timeout")
                continue
            frames.append(frame)
            spoken_s += frame_s
            if state == "end" or spoken_s >= self.max_utterance_s:
                break
        if not frames:
            return self._empty_listen("stopped")
        data = np.concatenate([np.asarray(f, dtype=np.float32).reshape(-1) for f in frames])
        self._publish("voice.state", state="thinking")
        result = await self.transcriber.transcribe(data, sample_rate=self.sample_rate)
        self._publish("voice.stt.done", ok=bool(result.get("ok")),
                      chars=len(result.get("text", "")), reason=result.get("reason", ""))
        log.info("capture_utterance: heard %r (%.2fs)", result.get("text", ""), spoken_s)
        return result

    def _get_frame_blocking(self, timeout: float = 1.0):
        if self.mic is None:
            return None
        try:
            return self.mic.get_frame(timeout)
        except Exception:
            return None

    async def _listen_loop(self) -> None:
        log.info("voice wake loop started (wake=%s)", self.wake.model_name)
        self._state = "listening"
        try:
            loop = asyncio.get_running_loop()
            # Rolling buffer of the last ~2.4s so a command spoken immediately
            # after "jarvis" (before the wake model finished firing) is not lost.
            pre_roll: deque[Any] = deque(maxlen=80)
            while not self._stopping:
                try:
                    if self.mic is None:
                        await asyncio.sleep(0.5)
                        continue
                    frame = await loop.run_in_executor(None, self._get_frame_blocking, 1.0)
                    if frame is None:
                        continue
                    pre_roll.append(frame)
                    if self.muted:
                        continue
                    try:
                        fired = self.wake.feed(frame)
                    except Exception as exc:
                        log.error("wake.feed error: %s", exc)
                        continue
                    if not fired:
                        continue
                    log.info("wake word fired")
                    self._publish("voice.wake", model=self.wake.model_name)
                    # Drop the wake word itself (and any lead-up) before we act,
                    # so it can never be re-transcribed as the user's command.
                    self._drain_mic()
                    try:
                        v = self.voice_config
                        if v is not None and getattr(v, "wake_sound", True):
                            await self.say("Yes sir", play=True)
                    except Exception as exc:
                        log.debug("wake sound failed: %s", exc)
                    # Drop the wake-sound echo so "Yes sir" is not captured as
                    # the command either; then listen with a clean mic.
                    self._drain_mic()
                    self.wake.reset()
                    try:
                        await self._conversation(list(pre_roll))
                    except Exception as exc:
                        log.error("conversation error: %s", exc)
                    self.wake.reset()
                    pre_roll.clear()
                    self._publish("voice.state", state="idle")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("wake loop iteration error: %s", exc)
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - loop must not crash core
            log.error("voice loop error: %s", exc)
            self._state_reason = f"loop error: {exc}"
            self._publish("voice.status", state="error", reason=str(exc))
        finally:
            self._state = "stopped"
            log.info("voice wake loop stopped")

    async def _conversation(self, pre_roll: list[Any] | None = None) -> int:
        """Wake → utterance → reply, then follow-ups until a silent timeout."""
        turns = 0
        wait_s = 5.0                       # first utterance right after the wake word
        while not self._stopping and not self.muted:
            captured = await self.capture_utterance(wait_s, pre_roll=pre_roll)
            pre_roll = None                 # only the first capture uses the buffer
            text = _strip_wake_word(captured.get("text") or "")
            if not text:
                break
            await self.handle_utterance(text)
            turns += 1
            self._drain_mic()              # never transcribe our own reply
            wait_s = self.conversation_timeout_s
        return turns
