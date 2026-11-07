"""Media worker: ``media.probe`` (ffprobe) and ``media.convert`` (ffmpeg transcode).

Requires ``ffmpeg``/``ffprobe`` on PATH (free/open source; e.g. ``winget install
Gyan.FFmpeg`` or ``apt install ffmpeg``); otherwise an honest "unavailable".

Both tools reuse the file boundary from ``file_ops`` (allowed roots + protected
roots). Paths are handed to ffmpeg with the ``file:`` protocol prefix so a file
name can never be interpreted as another ffmpeg protocol (``concat:``,
``http:`` ...). ``media.convert`` exposes a fixed set of options (codec enums,
CRF, audio bitrate, trim) rather than raw ffmpeg arguments, writes to a temp
sibling, probes it, then places it with a no-clobber rename. ``verify``
re-probes the output (real read-back: container parses, streams present,
duration > 0, SHA-256 unchanged).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from ...sandbox.limits import Limits
from . import _common, _win
from .file_ops import _err, _guard, _place_no_clobber, _remove_quiet, _tmp_sibling
from .files import _sha256_file

log = get_logger("jarvis.tools.ffmpeg")

__all__ = ["MediaProbeTool", "MediaConvertTool", "build_convert_argv", "health",
           "OUTPUT_EXTS", "VIDEO_CODECS", "AUDIO_CODECS"]

OUTPUT_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".aac", ".wav", ".flac",
               ".ogg", ".opus", ".gif")
VIDEO_CODECS = ("auto", "copy", "libx264", "libx265", "libvpx-vp9", "none")
AUDIO_CODECS = ("auto", "copy", "aac", "libmp3lame", "libopus", "libvorbis", "flac",
                "pcm_s16le", "none")
_BITRATE = re.compile(r"^\d{2,3}k$")
MAX_TIMEOUT_S = 3600


def health() -> dict[str, Any]:
    missing = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    if missing:
        return _common.health_fail(f"{' and '.join(missing)} not found on PATH; install FFmpeg "
                                   "(winget install Gyan.FFmpeg / apt install ffmpeg)")
    return _common.health_ok("ffmpeg + ffprobe on PATH")


async def _exec(argv: list[str], timeout: float,
                sandbox: Any | None = None) -> tuple[int | None, str, str]:
    """Run ffmpeg/ffprobe inside the sandbox (process tree killed together, env scrubbed).

    Resource *caps* are deliberately disabled here (a legitimate transcode may use
    far more RAM and processes than a shell command); the containment that matters
    for a media worker is that the tree dies with the parent and never sees a
    secret, not throttling it. ``shell.run`` remains the capped path.
    """
    if sandbox is None:
        sandbox = _sandbox(None)
    lim = Limits(max_memory_mb=0, max_processes=0, max_cpu_seconds=0, max_file_mb=0,
                 wall_timeout_s=timeout)
    res = await sandbox.run_async(argv, timeout=timeout, limits=lim)
    if res.timed_out:
        return None, res.stdout, res.error or f"timed out after {timeout:g}s"
    if res.exit_code is None:
        return None, res.stdout, res.error or "process did not start"
    return res.exit_code, res.stdout, res.stderr


def _sandbox(ctx: ToolContext | None) -> Any:
    """The kernel's sandbox service, or a config-bound one if none is wired."""
    svc = ctx.service("sandbox") if ctx is not None else None
    if svc is not None:
        return svc
    from ...sandbox import SandboxService
    return SandboxService(config=getattr(ctx, "config", None))


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def probe(path: Path, timeout: float = 60.0,
                sandbox: Any | None = None) -> dict[str, Any]:
    """Structured ffprobe summary. Raises RuntimeError on failure."""
    code, out, err = await _exec(["ffprobe", "-v", "error", "-print_format", "json",
                                  "-show_format", "-show_streams", f"file:{path}"],
                                 timeout, sandbox=sandbox)
    if code != 0:
        raise RuntimeError(f"ffprobe failed: {err.strip()[-300:] or code}")
    raw = json.loads(out or "{}")
    fmt = raw.get("format") or {}
    streams = []
    for s in raw.get("streams") or []:
        streams.append({k: s.get(k) for k in ("index", "codec_type", "codec_name", "width",
                                              "height", "sample_rate", "channels",
                                              "pix_fmt", "r_frame_rate") if s.get(k) is not None}
                       | {"duration_s": _num(s.get("duration"))})
    return {"path": str(path), "format": fmt.get("format_name"),
            "duration_s": _num(fmt.get("duration")), "size_bytes": int(_num(fmt.get("size")) or 0),
            "bit_rate": int(_num(fmt.get("bit_rate")) or 0) or None,
            "stream_count": len(streams), "streams": streams}


@register(["ffprobe", "media.info"])
class MediaProbeTool(BaseTool):
    spec = ToolSpec(
        name="media.probe",
        summary="Inspect an audio/video file with ffprobe (format, duration, streams).",
        category="media",
        side_effect=SideEffect.NONE,
        params=(ToolParam("path", "string", "Media file inside the allowed roots.",
                          required=True),),
        returns="object: {format, duration_s, size_bytes, bit_rate, streams:[...]}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _common.unavailable_result(health())
        if gate:
            return gate
        try:
            path = _guard(ctx, kwargs.get("path"), "path")
            if not path.is_file():
                return ToolResult(ok=False, error=f"not a file: {path}")
            return ToolResult(ok=True, data=await probe(path, sandbox=_sandbox(ctx)))
        except RuntimeError as exc:
            return ToolResult(ok=False, error=str(exc))
        except Exception as exc:
            return _err(exc)


def build_convert_argv(src: Path, out: Path, opts: dict[str, Any]) -> list[str]:
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-n",
            "-i", f"file:{src}"]
    if opts.get("max_duration_s"):
        argv += ["-t", f"{opts['max_duration_s']:g}"]
    vc = opts.get("video_codec", "auto")
    if vc == "none":
        argv.append("-vn")
    elif vc != "auto":
        argv += ["-c:v", vc]
    if opts.get("crf") is not None and vc not in ("copy", "none"):
        argv += ["-crf", str(opts["crf"])]
    ac = opts.get("audio_codec", "auto")
    if ac == "none":
        argv.append("-an")
    elif ac != "auto":
        argv += ["-c:a", ac]
    if opts.get("audio_bitrate") and ac not in ("copy", "none"):
        argv += ["-b:a", opts["audio_bitrate"]]
    argv.append(f"file:{out}")
    return argv


def _convert_opts(kwargs: dict[str, Any]) -> dict[str, Any]:
    vc = str(kwargs.get("video_codec") or "auto")
    ac = str(kwargs.get("audio_codec") or "auto")
    if vc not in VIDEO_CODECS:
        raise ValueError(f"video_codec must be one of {', '.join(VIDEO_CODECS)}")
    if ac not in AUDIO_CODECS:
        raise ValueError(f"audio_codec must be one of {', '.join(AUDIO_CODECS)}")
    if vc == "none" and ac == "none":
        raise ValueError("video_codec and audio_codec cannot both be none")
    crf = _win.int_arg(kwargs, "crf")
    if crf is not None and not 0 <= crf <= 51:
        raise ValueError("crf must be 0-51")
    br = str(kwargs.get("audio_bitrate") or "").strip().lower()
    if br and not _BITRATE.match(br):
        raise ValueError("audio_bitrate must look like '128k'")
    dur = kwargs.get("max_duration_s")
    if dur not in (None, ""):
        try:
            dur = float(dur)
        except (TypeError, ValueError):
            raise ValueError("max_duration_s must be a number") from None
        if not 0 < dur <= 86400:
            raise ValueError("max_duration_s must be between 0 and 86400")
    else:
        dur = None
    try:
        timeout = float(kwargs.get("timeout_s") or 600)
    except (TypeError, ValueError):
        raise ValueError("timeout_s must be a number") from None
    return {"video_codec": vc, "audio_codec": ac, "crf": crf, "audio_bitrate": br or None,
            "max_duration_s": dur, "timeout_s": max(5.0, min(timeout, MAX_TIMEOUT_S))}


@register(["ffmpeg", "media.transcode"])
class MediaConvertTool(BaseTool):
    spec = ToolSpec(
        name="media.convert",
        summary="Transcode audio/video with ffmpeg into a new file (never overwrites).",
        category="media",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("input", "string", "Source media file.", required=True),
            ToolParam("output", "string",
                      f"New output file; extension picks the container ({', '.join(OUTPUT_EXTS)}).",
                      required=True),
            ToolParam("video_codec", "string", "Video codec.", default="auto", enum=VIDEO_CODECS),
            ToolParam("audio_codec", "string", "Audio codec.", default="auto", enum=AUDIO_CODECS),
            ToolParam("crf", "integer", "Quality for x264/x265/vp9 (0-51, lower = better)."),
            ToolParam("audio_bitrate", "string", "e.g. '192k'."),
            ToolParam("max_duration_s", "number", "Only convert the first N seconds."),
            ToolParam("timeout_s", "number", f"Kill ffmpeg after this long (max {MAX_TIMEOUT_S}).",
                      default=600),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {input, output, sha256, probe}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _common.unavailable_result(health())
        if gate:
            return gate
        tmp: Path | None = None
        try:
            src = _guard(ctx, kwargs.get("input"), "input")
            out = _guard(ctx, kwargs.get("output"), "output")
            if not src.is_file():
                return ToolResult(ok=False, error=f"input not found: {src}")
            if out.suffix.lower() not in OUTPUT_EXTS:
                return ToolResult(ok=False, error=f"output extension must be one of {OUTPUT_EXTS}")
            if out.exists():
                return ToolResult(ok=False, error=f"output already exists (no overwrite): {out}")
            if not out.parent.is_dir():
                return ToolResult(ok=False, error=f"output folder does not exist: {out.parent}")
            opts = _convert_opts(kwargs)
            argv = build_convert_argv(src, out, opts)
            if _common.dry_run_requested(kwargs):
                return _common.preview(target={"input": str(src), "output": str(out)},
                                       operation="media.convert",
                                       effect=f"create {out} by transcoding {src}; input unchanged",
                                       reversible=True, rollback=f"files.delete path={out}",
                                       argv=argv)
            tmp = _tmp_sibling(out)
            argv[-1] = f"file:{tmp}"
            sandbox = _sandbox(ctx)
            code, _stdout, err = await _exec(argv, opts["timeout_s"], sandbox=sandbox)
            if code != 0:
                return ToolResult(ok=False, error=f"ffmpeg failed: {err.strip()[-400:] or code}",
                                  data={"argv": argv})
            info = await probe(tmp, sandbox=sandbox)
            if not info["stream_count"]:
                return ToolResult(ok=False, error="ffmpeg produced a file with no streams")
            _place_no_clobber(tmp, out)
            tmp = None
            info["path"] = str(out)
            digest, size = _sha256_file(out)
            return ToolResult(ok=True, data={"input": str(src), "output": str(out),
                                             "sha256": digest, "size": size, "probe": info},
                              evidence={"method": "ffmpeg+ffprobe"}, certainty="unverified")
        except RuntimeError as exc:
            return ToolResult(ok=False, error=str(exc))
        except Exception as exc:
            log.warning("media.convert failed: %s", exc)
            return _err(exc)
        finally:
            if tmp is not None:
                _remove_quiet(tmp)

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        out = Path(result.data["output"])
        try:
            info = await probe(out, sandbox=_sandbox(ctx))
            digest, _ = await asyncio.to_thread(_sha256_file, out)
        except Exception as exc:
            return {"verified": False, "method": "ffprobe-readback", "note": str(exc)}
        ok = bool(info["stream_count"]) and (info["duration_s"] or 0) > 0 \
            and digest == result.data.get("sha256")
        return {"verified": ok, "method": "ffprobe-readback+sha256",
                "duration_s": info["duration_s"], "streams": info["stream_count"]}
