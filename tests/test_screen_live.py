"""Live screen reader: sampler, HTTP contract and the ``vision.live`` tool.

Nothing here captures the real desktop: the sampler is given a fake grab/OCR,
and the frame endpoint is exercised with an in-memory image. The real capture
path is verified separately with ``python -m jarvis.screen_live --once``.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from PIL import Image

import jarvis.screen_live as screen_live
from jarvis.screen_live import ScreenSampler
from jarvis.tools import REGISTRY
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import vision_tool  # noqa: F401  (registers vision.*)


def _fake_ocr(text: str = "known text"):
    def ocr(image):
        return {"lines": [{"text": text, "confidence": 0.99,
                           "box": [[0, 0], [10, 10]]}],
                "text": text}
    return ocr


def _grab(_all_screens):
    return Image.new("RGB", (32, 24), (10, 20, 30))


@pytest.fixture
def live_server():
    sampler = ScreenSampler(interval=0.5, grab=_grab, ocr=_fake_ocr())
    sampler.sample_once()
    server = ThreadingHTTPServer(("127.0.0.1", 0), screen_live._Handler)
    server.sampler = sampler
    server.token = None
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, sampler, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        sampler.stop()


def _get(url: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_sampler_keeps_only_the_latest_sample():
    texts = iter(["first", "second"])
    sampler = ScreenSampler(interval=0.5, grab=_grab,
                            ocr=lambda image: _fake_ocr(next(texts))(image))
    sampler.sample_once()
    sampler.sample_once()
    assert sampler.samples == 2
    assert sampler.latest["text"] == "second"
    assert sampler.error is None


def test_latest_endpoint_returns_text_and_metadata(live_server):
    _server, _sampler, url = live_server
    status, payload = _get(url + "/latest")
    assert status == 200
    assert payload["ok"] is True
    assert payload["text"] == "known text"
    assert payload["line_count"] == 1
    assert payload["age_s"] >= 0
    assert "window" in payload


def test_health_reports_sampling_state(live_server):
    _server, _sampler, url = live_server
    status, payload = _get(url + "/health")
    assert status == 200
    assert payload["ok"] is True
    assert payload["samples"] >= 1
    assert payload["errors"] == 0


def test_frame_endpoint_returns_a_current_jpeg(live_server, monkeypatch):
    monkeypatch.setattr(screen_live, "_grab_screen", _grab)
    _server, _sampler, url = live_server
    with urllib.request.urlopen(url + "/frame", timeout=5) as response:
        body = response.read()
        assert response.status == 200
        assert response.headers["Content-Type"] == "image/jpeg"
    assert body.startswith(b"\xff\xd8")          # JPEG SOI
    assert response.headers["Cache-Control"] == "no-store"


def test_token_is_enforced_when_configured(live_server):
    server, _sampler, url = live_server
    server.token = "s3cret"
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(url + "/latest")
    assert exc.value.code == 401
    status, payload = _get(url + "/latest",
                           {"X-Jarvis-Screen-Token": "s3cret"})
    assert status == 200 and payload["ok"] is True


async def test_tool_reads_the_live_text(live_server, tmp_path, monkeypatch):
    _server, _sampler, url = live_server
    monkeypatch.setenv("JARVIS_SCREEN_LIVE_URL", url)
    ctx = ToolContext(config=None, db=None, bus=None)
    result = await REGISTRY.get("vision.live").run(ctx)
    assert result.ok, result.error
    assert result.data["text"] == "known text"
    assert result.data["line_count"] == 1
    assert result.data["age_s"] >= 0


async def test_tool_reports_a_reader_that_is_not_running(monkeypatch):
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    ctx = ToolContext(config=None, db=None, bus=None)
    result = await REGISTRY.get("vision.live").run(
        ctx, url=f"http://127.0.0.1:{port}")
    assert result.ok is False
    assert "not reachable" in (result.error or "")
    assert "jarvis.screen_live" in (result.error or "")


def test_describe_reports_a_missing_vision_model(live_server, monkeypatch):
    monkeypatch.setattr(screen_live, "_grab_screen", _grab)

    def fail(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(screen_live, "describe", fail)
    _server, _sampler, url = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(url + "/describe")
    assert exc.value.code == 503
    body = json.loads(exc.value.read().decode("utf-8"))
    assert "vision model unavailable" in body["error"]
    assert "llava:7b" in body["error"]


def test_vision_live_is_auto_allowed_by_default():
    """Read-only, local, same category as vision.screen: no approval prompt."""
    from jarvis.config import PolicyConfig
    assert "vision.live" in PolicyConfig().auto_allow
