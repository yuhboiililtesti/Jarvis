"""``web.open`` - open an http(s) URL in the owner's default browser (LOCAL_EXEC).

Only ``http``/``https`` URLs with a host are accepted; URLs carrying embedded
credentials (``user:pass@host``), control characters or more than
:data:`MAX_URL_LEN` characters are refused. ``file:``, ``javascript:``,
``ms-settings:`` and every other scheme are refused, because the OS shell would
otherwise launch arbitrary handlers.

There is no independent read-back that a browser tab actually opened, so
``verify`` returns ``None`` (unverified).
"""

from __future__ import annotations

import asyncio
import webbrowser
from typing import Any
from urllib.parse import urlsplit

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common

log = get_logger("jarvis.tools.browser")

__all__ = ["WebOpenTool", "check_open_url", "MAX_URL_LEN", "health"]

MAX_URL_LEN = 2048


def health() -> dict[str, Any]:
    try:
        webbrowser.get()
    except webbrowser.Error as exc:
        return _common.health_fail(f"no default browser is registered: {exc}")
    return _common.health_ok("python webbrowser -> OS default browser")


def check_open_url(url: str) -> str | None:
    """Return an error string, or None when the URL is acceptable."""
    if not url:
        return "url is required"
    if len(url) > MAX_URL_LEN:
        return f"url longer than {MAX_URL_LEN} characters"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in url) or " " in url:
        return "url contains whitespace or control characters"
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return f"malformed url: {exc}"
    if parts.scheme.lower() not in ("http", "https"):
        return f"only http/https URLs can be opened (got '{parts.scheme or '(none)'}')"
    if not parts.hostname:
        return "url has no host"
    if parts.username is not None or parts.password is not None:
        return "refused: url embeds credentials (user:pass@host)"
    return None


def _open_url(url: str) -> bool:
    return bool(webbrowser.open(url, new=2))


@register(["open_url", "browser.open"])
class WebOpenTool(BaseTool):
    spec = ToolSpec(
        name="web.open",
        summary="Open an http(s) URL in the default browser. Does NOT launch desktop apps; use apps.launch for those.",
        category="web",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(ToolParam("url", "string", "Absolute http(s) URL to open.", required=True),
                _common.DRY_RUN_PARAM),
        returns="object: {url, opened}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            url = str(kwargs.get("url") or "").strip()
            bad = check_open_url(url)
            if bad:
                return ToolResult(ok=False, error=bad,
                                  evidence={"refused": True} if bad.startswith("refused") else {})
            if _common.dry_run_requested(kwargs):
                return _common.preview(target=url, operation="web.open",
                                       effect="a new tab opens in the default browser",
                                       reversible=True, rollback="close the tab")
            gate = _common.unavailable_result(health())
            if gate:
                return gate
            opened = await asyncio.to_thread(_open_url, url)
            if not opened:
                return ToolResult(ok=False, error="the OS reported that no browser could be launched")
            return ToolResult(ok=True, data={"url": url, "opened": True},
                              evidence={"method": "webbrowser.open"}, certainty="unverified")
        except Exception as exc:
            log.warning("web.open failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")

    def preflight(self, ctx: ToolContext, **kwargs: Any):
        """Reject a non-http(s) URL before the owner is asked to approve it."""
        bad = check_open_url(str(kwargs.get("url") or "").strip())
        if bad:
            return ToolResult(ok=False, error=bad, certainty="settled")
        return None

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        return None  # no way to observe the browser tab independently
