"""``jarvis.screen_live`` — live, in-memory screen text reader.

A small loopback service that samples the screen, OCRs it locally, and keeps
**only the latest result in memory**. Nothing is written to disk, no frames are
retained, and no history is kept: this is a live view, not a recording.

Endpoints (``127.0.0.1:8799`` by default):

* ``GET /health``   — sampler state, interval, sample/error counts, engine
* ``GET /latest``   — the latest OCR text, window title, timestamp, latency
* ``GET /frame``    — a **current** JPEG captured on request (never stored)
* ``GET /describe`` — a vision-model caption of the current frame (if a VLM is
  configured; otherwise a clear 503)

Local only: it binds loopback, and an optional ``JARVIS_SCREEN_TOKEN`` must be
presented as ``X-Jarvis-Screen-Token``. Start it with::

    python -m jarvis.screen_live --interval 1.5

Stop it with Ctrl+C (or ``--once`` for a single sample printed as JSON).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .logging_setup import get_logger

log = get_logger("jarvis.screen_live")

__all__ = ["ScreenSampler", "serve", "main", "DEFAULT_PORT", "DEFAULT_INTERVAL",
           "VLM_URL", "VLM_MODEL"]

DEFAULT_PORT = 8799
DEFAULT_INTERVAL = 1.5
MAX_FRAME_JPEG = 4 * 1024 * 1024
VLM_URL = os.environ.get("JARVIS_SCREEN_VLM_URL", "http://127.0.0.1:11435")
VLM_MODEL = os.environ.get("JARVIS_SCREEN_VLM_MODEL", "llava:7b")


_DXCAM: Any = None
_DXCAM_FAILED = False
_DXCAM_LAST: Any = None
_USER32_CURSOR = None


def _cursor_position() -> tuple[int, int] | None:
    """The real pointer position (DXGI/GDI captures omit the cursor)."""
    global _USER32_CURSOR
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt
        if _USER32_CURSOR is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
            user32.GetCursorPos.restype = wt.BOOL
            _USER32_CURSOR = user32
        point = wt.POINT()
        if not _USER32_CURSOR.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)
    except Exception:
        return None


def _mark_cursor(image: Any):
    """Draw a visible marker at the true pointer position.

    This is an explicit marker, not the OS cursor bitmap: screen grabs omit the
    pointer, and a marker is honest about where it is.
    """
    position = _cursor_position()
    if position is None:
        return image
    from PIL import ImageDraw
    draw = ImageDraw.Draw(image)
    x, y = position
    radius = 10
    draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                 outline=(255, 70, 70), width=2)
    draw.line([x - radius - 6, y, x - radius, y], fill=(255, 255, 255), width=1)
    draw.line([x + radius, y, x + radius + 6, y], fill=(255, 255, 255), width=1)
    draw.line([x, y - radius - 6, x, y - radius], fill=(255, 255, 255), width=1)
    draw.line([x, y + radius, x, y + radius + 6], fill=(255, 255, 255), width=1)
    return image


def _grab_screen(all_screens: bool):
    """Current screen as a PIL image.

    Uses DXGI Desktop Duplication (``dxcam``) for the primary monitor when
    available — it also works for borderless games, where GDI ``BitBlt`` can
    return black — and falls back to Pillow. ``JARVIS_SCREEN_CAPTURE=pil``
    forces the fallback.
    """
    global _DXCAM, _DXCAM_FAILED, _DXCAM_LAST
    if (sys.platform == "win32" and not all_screens and not _DXCAM_FAILED
            and os.environ.get("JARVIS_SCREEN_CAPTURE", "").lower() != "pil"):
        try:
            import dxcam
            if _DXCAM is None:
                _DXCAM = dxcam.create(output_color="RGB")
            if _DXCAM is not None:
                frame = _DXCAM.grab()
                if frame is not None:
                    _DXCAM_LAST = frame
                if _DXCAM_LAST is not None:
                    # dxcam returns None when nothing changed; repeating the
                    # last frame is correct (and avoids a 150 ms GDI capture).
                    from PIL import Image
                    return Image.fromarray(_DXCAM_LAST)
        except Exception:
            _DXCAM_FAILED = True
            log.warning("DXGI capture unavailable; using Pillow instead")
    from PIL import ImageGrab
    if all_screens:
        try:
            return ImageGrab.grab(all_screens=True)
        except TypeError:  # older Pillow
            return ImageGrab.grab()
    return ImageGrab.grab()


def _ocr_image(image: Any) -> dict[str, Any]:
    import numpy as np
    from .tools.builtin.vision_tool import _ocr

    array = np.array(image.convert("RGB"))[:, :, ::-1]  # RapidOCR wants BGR
    return _ocr(array)


_USER32 = None


def _foreground_window() -> dict[str, Any] | None:
    """The focused window on Windows; ``None`` elsewhere (never guessed)."""
    global _USER32
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt
        if _USER32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetForegroundWindow.restype = wt.HWND
            user32.GetWindowTextLengthW.argtypes = [wt.HWND]
            user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
            user32.GetWindowThreadProcessId.argtypes = [
                wt.HWND, ctypes.POINTER(wt.DWORD)]
            _USER32 = user32
        hwnd = _USER32.GetForegroundWindow()
        if not hwnd:
            return None
        length = _USER32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        _USER32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wt.DWORD()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {"title": buffer.value, "pid": int(pid.value)}
    except Exception:  # pragma: no cover - title is informative, never critical
        return None


class ScreenSampler:
    """Samples the screen on a timer and keeps only the latest OCR result."""

    def __init__(self, *, interval: float = DEFAULT_INTERVAL,
                 all_screens: bool = False,
                 grab: Callable[[bool], Any] | None = None,
                 ocr: Callable[[Any], dict[str, Any]] | None = None) -> None:
        self.interval = max(0.5, float(interval))
        self.all_screens = bool(all_screens)
        self._grab = grab or _grab_screen
        self._ocr = ocr or _ocr_image
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.latest: dict[str, Any] | None = None
        self.error: str | None = None
        self.samples = 0
        self.errors = 0
        self.enabled = True
        self.started_at = time.time()

    # -- sampling --------------------------------------------------------
    def sample_once(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            image = self._grab(self.all_screens)
            out = self._ocr(image)
            lines = list(out.get("lines") or [])
            record = {
                "ts": time.time(),
                "text": "\n".join(line["text"] for line in lines),
                "lines": lines,
                "line_count": len(lines),
                "width": int(getattr(image, "width", 0)),
                "height": int(getattr(image, "height", 0)),
                "window": _foreground_window(),
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }
            with self._lock:
                self.latest = record
                self.error = None
                self.samples += 1
            return record
        except Exception as exc:
            with self._lock:
                self.errors += 1
                self.error = f"{type(exc).__name__}: {exc}"
            raise

    def _loop(self) -> None:
        logged = False
        while not self._stop.is_set() and self.enabled:
            try:
                self.sample_once()
                logged = False
            except Exception:
                if not logged:
                    log.warning("screen sample failed: %s", self.error)
                    logged = True
            self._stop.wait(self.interval)

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="screen-live-sampler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    # -- capture switch ---------------------------------------------------
    def pause(self) -> None:
        """Stop sampling and forget the last sample (no screen data is served)."""
        with self._lock:
            self.enabled = False
            self.latest = None
            self.error = None
        self.stop()
        log.info("screen capture off")

    def resume(self) -> None:
        with self._lock:
            self.enabled = True
            self.error = None
        self.start()
        log.info("screen capture on")

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {"ok": self.error is None and self.latest is not None,
                    "capturing": self.enabled,
                    "engine": "rapidocr (ONNX, cpu, local)",
                    "interval_s": self.interval,
                    "all_screens": self.all_screens,
                    "samples": self.samples,
                    "errors": self.errors,
                    "error": self.error,
                    "started_at": self.started_at,
                    "latest_ts": (self.latest or {}).get("ts"),
                    "uptime_s": round(time.time() - self.started_at, 1)}


def _encode_jpeg(image: Any, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def frame_jpeg(all_screens: bool = False, quality: int = 70,
               max_width: int | None = None,
               cursor: bool = True) -> bytes:
    image = _grab_screen(all_screens)
    if max_width and image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, max(1, int(image.height * ratio))))
    if cursor:
        image = _mark_cursor(image.convert("RGB"))
    return _encode_jpeg(image, quality)


def describe(jpeg: bytes, question: str = "", *,
             url: str = VLM_URL, model: str = VLM_MODEL,
             timeout: float = 120.0) -> str:
    """Ask a local vision model to caption the frame.

    The prompt is deliberately strict: small VLMs readily invent scenes, so the
    caption is a *guess* and is always returned alongside grounded data (the
    active window title and OCR text) by ``/describe``.
    """
    prompt = (question.strip() or
              "In one or two sentences, describe what is on this screen. If it "
              "is a game, name it and say what is happening. Only state what is "
              "clearly visible; if you are unsure, say so.")
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt,
             "images": [base64.b64encode(jpeg).decode("ascii")]},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "repeat_penalty": 1.05,
                    "num_predict": 128},
    }
    request = urllib.request.Request(
        url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return str((body.get("message") or {}).get("content") or "").strip()


class _Handler(BaseHTTPRequestHandler):
    server_version = "jarvis-screen-live/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------
    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        token = getattr(self.server, "token", None)
        return not token or \
            self.headers.get("X-Jarvis-Screen-Token") == token

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        sampler: ScreenSampler = self.server.sampler  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        path = parsed.path
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        if path in ("/latest", "/frame", "/describe", "/stream.mjpg") and not sampler.enabled:
            self._json(409, {"ok": False, "capturing": False,
                             "error": "capture is off"})
            return
        if path == "/health":
            self._json(200, sampler.health())
            return
        if path == "/latest":
            with sampler._lock:
                latest = dict(sampler.latest) if sampler.latest else None
                error = sampler.error
            if latest is None:
                self._json(503, {"ok": False,
                                 "error": error or "no sample yet"})
                return
            latest["age_s"] = round(time.time() - latest["ts"], 1)
            latest["ok"] = True
            self._json(200, latest)
            return
        if path == "/frame":
            query = parse_qs(parsed.query)
            try:
                quality = max(30, min(int((query.get("quality") or ["70"])[0]), 95))
            except ValueError:
                quality = 70
            try:
                scale = int((query.get("scale") or ["0"])[0])
            except ValueError:
                scale = 0
            cursor = (query.get("cursor") or ["1"])[0].lower() not in ("0", "false", "no")
            try:
                raw = frame_jpeg(sampler.all_screens, quality=quality,
                                 max_width=scale or None, cursor=cursor)
            except Exception as exc:
                self._json(503, {"ok": False,
                                 "error": f"capture failed: {type(exc).__name__}: {exc}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/stream.mjpg":
            query = parse_qs(parsed.query)
            try:
                fps = float((query.get("fps") or ["15"])[0])
            except ValueError:
                fps = 15.0
            fps = max(1.0, min(fps, 30.0))
            try:
                scale = int((query.get("scale") or ["1280"])[0])
            except ValueError:
                scale = 1280
            try:
                quality = int((query.get("quality") or ["55"])[0])
            except ValueError:
                quality = 55
            quality = max(30, min(quality, 90))
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            next_frame = time.perf_counter()
            try:
                while True:
                    raw = frame_jpeg(sampler.all_screens, quality=quality,
                                     max_width=scale or None, cursor=True)
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: " +
                                     str(len(raw)).encode("ascii") + b"\r\n\r\n")
                    self.wfile.write(raw)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    next_frame += 1.0 / fps
                    delay = next_frame - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        next_frame = time.perf_counter()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if path == "/describe":
            query = parse_qs(parsed.query)
            question = (query.get("q") or [""])[0]
            try:
                raw = frame_jpeg(sampler.all_screens)
                text = describe(raw, question)
            except Exception as exc:
                self._json(503, {
                    "ok": False,
                    "error": (f"vision model unavailable at {VLM_URL} "
                              f"({type(exc).__name__}: {exc}); start Ollama and "
                              f"pull '{VLM_MODEL}'"),
                })
                return
            self._json(200, {
                "ok": True,
                "description": text,
                "kind": "vision-guess",
                "model": VLM_MODEL,
                "window": (sampler.latest or {}).get("window"),
                "text": ((sampler.latest or {}).get("text") or "")[:1500],
            })
            return
        if path == "/":
            self._json(200, {
                "service": "jarvis screen live",
                "endpoints": ["/health", "/latest", "/frame", "/stream.mjpg",
                              "/describe", "POST /capture/on",
                              "POST /capture/off"],
                "recording": False,
            })
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        sampler: ScreenSampler = self.server.sampler  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        if path == "/capture/on":
            sampler.resume()
            self._json(200, {"ok": True, "capturing": True})
            return
        if path == "/capture/off":
            sampler.pause()
            self._json(200, {"ok": True, "capturing": False})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("screen-live %s - %s", self.address_string(), fmt % args)


class _QuietServer(ThreadingHTTPServer):
    """A dropped client connection is normal (health probes, page reloads)."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def serve(port: int = DEFAULT_PORT, *, interval: float = DEFAULT_INTERVAL,
          all_screens: bool = False, token: str | None = None,
          block: bool = True, start_paused: bool = False) -> ThreadingHTTPServer:
    sampler = ScreenSampler(interval=interval, all_screens=all_screens)
    if start_paused:
        sampler.pause()
    else:
        sampler.start()
    server = _QuietServer(("127.0.0.1", int(port)), _Handler)
    server.sampler = sampler  # type: ignore[attr-defined]
    server.token = token  # type: ignore[attr-defined]
    server.daemon_threads = True
    if not block:
        return server
    try:
        log.info("screen live on http://127.0.0.1:%s (in-memory, no recording)",
                 port)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sampler.stop()
        server.server_close()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis.screen_live",
        description="Live in-memory screen text (local OCR; nothing recorded).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between samples (min 0.5)")
    parser.add_argument("--all-screens", action="store_true")
    parser.add_argument("--once", action="store_true",
                        help="take one sample, print it as JSON, exit")
    parser.add_argument("--start-paused", action="store_true",
                        help="serve but do not sample until POST /capture/on")
    args = parser.parse_args(argv)
    token = os.environ.get("JARVIS_SCREEN_TOKEN") or None
    if args.once:
        sampler = ScreenSampler(interval=args.interval,
                                all_screens=args.all_screens)
        try:
            record = sampler.sample_once()
        except Exception as exc:
            print(json.dumps({"ok": False,
                              "error": f"{type(exc).__name__}: {exc}"}))
            return 1
        print(json.dumps({"ok": True, "text": record["text"],
                          "line_count": record["line_count"],
                          "width": record["width"], "height": record["height"],
                          "window": record["window"],
                          "latency_ms": record["latency_ms"]}))
        return 0
    serve(args.port, interval=args.interval, all_screens=args.all_screens,
          token=token, start_paused=args.start_paused)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
