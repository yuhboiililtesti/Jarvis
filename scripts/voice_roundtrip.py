"""Voice round-trip: synthesise a phrase, process it, transcribe it back.

Proves the TTS -> effect -> STT chain with a real audio file rather than a
claim. Everything runs on the CPU and writes files only; nothing is played.

    .venv\\Scripts\\python.exe scripts\\voice_roundtrip.py
    .venv\\Scripts\\python.exe scripts\\voice_roundtrip.py --engine piper --effect none
    .venv\\Scripts\\python.exe scripts\\voice_roundtrip.py --samples   # audition WAVs

Options:
  --engine kokoro|piper   engine to use (default: kokoro, no fallback)
  --voice  ID             kokoro voice (default bm_george) or piper voice
  --effect jarvis|none    post-processing (default jarvis)
  --strength 0..1         effect strength (default 0.5)
  --stt-model NAME        faster-whisper model (default base.en; cpu/int8)
  --offline               forbid model downloads (HF_HUB_OFFLINE=1)
  --samples               also write audition WAVs to data/artifacts/voice-samples/

The faster-whisper model must already be cached unless downloads are allowed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PHRASE = "Good evening, sir. All systems are online and standing by."
SAMPLE_LINE = ("Good evening, sir. All systems are online, and I have taken the "
               "liberty of warming up the coffee machine.")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def word_match(expected: str, heard: str) -> tuple[int, int, float]:
    """(matched, expected_count, ratio) via longest common subsequence of words."""
    a, b = _words(expected), _words(heard)
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a)):
        for j in range(len(b)):
            dp[i + 1][j + 1] = dp[i][j] + 1 if a[i] == b[j] else max(dp[i][j + 1], dp[i + 1][j])
    m = dp[len(a)][len(b)]
    return m, len(a), (m / len(a) if a else 0.0)


async def write_samples(speaker, out_dir: Path) -> list[dict]:
    from jarvis.voice.tts import KOKORO_BRITISH_VOICES
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for v in KOKORO_BRITISH_VOICES:
        for fx in ("jarvis", "none"):
            jobs.append(("kokoro", v["id"], fx, out_dir / f"kokoro-{v['id']}-{fx}.wav"))
    for fx in ("jarvis", "none"):
        jobs.append(("piper", "en_GB-alan-medium", fx, out_dir / f"piper-en_GB-alan-medium-{fx}.wav"))
    rows = []
    for engine, voice, fx, path in jobs:
        res = await speaker.synthesize(SAMPLE_LINE, path, engine=engine, voice=voice,
                                       effect=fx, strength=0.5)
        rows.append({"file": str(path.relative_to(ROOT)), "engine": res["engine"],
                     "voice": res.get("voice"), "effect": res.get("effect"),
                     "ok": res["ok"], "duration_s": res["duration_s"],
                     "bytes": path.stat().st_size if path.exists() else 0,
                     "reason": res.get("reason", "")})
        print(f"  {'ok ' if res['ok'] else 'ERR'} {path.name:44} {res['duration_s']:5.2f}s "
              f"{res['engine']} {res.get('reason', '')}")
    return rows


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default="kokoro", choices=["kokoro", "piper"])
    ap.add_argument("--voice", default=None)
    ap.add_argument("--effect", default="jarvis", choices=["jarvis", "none"])
    ap.add_argument("--strength", type=float, default=0.5)
    ap.add_argument("--stt-model", default="base.en")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--samples", action="store_true")
    args = ap.parse_args()
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"

    from jarvis.config import load_config
    from jarvis.voice.stt import Transcriber
    from jarvis.voice.tts import Speaker

    cfg = load_config()
    cfg.voice.stt_model = args.stt_model
    cfg.voice.stt_device = "cpu"            # never the GPU here
    cfg.voice.stt_compute_type = "int8"
    speaker = Speaker(cfg)
    voice = args.voice or ("bm_george" if args.engine == "kokoro" else cfg.voice.piper_voice)
    out_dir = ROOT / "data" / "artifacts" / "voice-samples"
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / f"roundtrip-{args.engine}-{voice}-{args.effect}.wav"

    print(f"synthesising with {args.engine}/{voice}, effect={args.effect} {args.strength} ...")
    t0 = time.perf_counter()
    tts = await speaker.synthesize(PHRASE, wav, engine=args.engine, voice=voice,
                                   effect=args.effect, strength=args.strength)
    t_tts = time.perf_counter() - t0
    print("tts:", json.dumps({k: tts.get(k) for k in ("ok", "engine", "voice", "effect",
                                                       "effect_strength", "duration_s",
                                                       "sample_rate", "reason")}))
    print(f"tts wall time: {t_tts:.2f}s (includes model load on first use)")
    if not tts.get("ok"):
        return 1

    stt = Transcriber(cfg)
    ok, reason = await stt.available()
    print("stt available:", ok, reason)
    if not ok:
        return 1
    print(f"transcribing with faster-whisper {stt.model_name} ({stt.device}, {stt.compute_type}) ...")
    t0 = time.perf_counter()
    result = await stt.transcribe(str(wav))
    t_stt = time.perf_counter() - t0
    heard = result.get("text", "")
    matched, total, ratio = word_match(PHRASE, heard)
    print("expected:", PHRASE)
    print("heard:   ", heard)
    print(f"word match: {matched}/{total} = {ratio:.0%} | stt wall time {t_stt:.2f}s | "
          f"reason: {result.get('reason') or '-'}")
    print("wav:", wav)

    if args.samples:
        print("\nwriting audition samples ->", out_dir)
        rows = await write_samples(speaker, out_dir)
        (out_dir / "samples.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0 if result.get("ok") and ratio >= 0.8 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
