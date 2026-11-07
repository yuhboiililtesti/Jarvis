"""web.fetch must not become a loopback/LAN/metadata SSRF primitive.

It is auto-allowed (no approval), so a prompt-injected page could otherwise ask
JARVIS to read its own API, the router, or a metadata endpoint and echo it back.
"""

from __future__ import annotations

import socket

from jarvis.tools import REGISTRY
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import web


def test_public_urls_are_allowed_without_dns():
    assert web._check_url("https://example.com/page") is None
    assert web._check_url("http://93.184.216.34/") is None


def test_loopback_private_and_metadata_are_refused():
    for url in (
        "http://127.0.0.1:8765/api/audit",
        "http://localhost:8765/",
        "http://[::1]:8765/",
        "http://192.168.1.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
    ):
        reason = web._check_url(url)
        assert reason, url
        assert "refused" in reason, (url, reason)


def test_private_fetch_can_be_opted_in_by_the_owner():
    assert web._check_url("http://127.0.0.1:8888/", allow_private=True) is None


def test_a_public_name_resolving_to_loopback_is_refused(monkeypatch):
    def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    reason = web._check_url("http://rebind.example/", resolve=True)
    assert reason and "non-public" in reason


def test_unresolvable_name_fails_closed(monkeypatch):
    def boom(*args, **kwargs):
        raise socket.gaierror("no dns")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    reason = web._check_url("http://no-such-host.example/", resolve=True)
    assert reason and "could not be resolved" in reason


async def test_fetch_refuses_loopback_before_making_a_request(cfg, monkeypatch):
    ctx = ToolContext(config=cfg, db=None, bus=None)

    def must_not_run(*args, **kwargs):
        raise AssertionError("no HTTP client should be created for a blocked URL")

    monkeypatch.setattr(web.httpx, "AsyncClient", must_not_run)
    r = await REGISTRY.call("web.fetch", ctx,
                            url="http://127.0.0.1:8765/api/audit")
    assert not r.ok
    assert "refused" in (r.error or "")
