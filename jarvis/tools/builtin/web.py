"""Web tools: fetch a page as text, and search an optional local provider.

`web.fetch` downloads a URL, strips it to readable text and caps the response
size, so a huge or hostile page cannot blow up the Core. `web.search` uses a
SearXNG instance if one is configured in the secret store (`searx_url`); with no
provider configured it says so plainly and returns no results rather than
inventing any.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.web")

__all__ = ["WebFetchTool", "WebSearchTool"]

_USER_AGENT = "Jarvis/1.0 (+local-first personal assistant)"
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head", "iframe"}
_BLOCK_TAGS = {"p", "br", "div", "li", "tr", "section", "article", "header",
               "footer", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}


class _HtmlToText(HTMLParser):
    """Minimal HTML -> (title, text) reducer; never executes anything."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._title: str | None = None
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title and self._title is None:
            stripped = data.strip()
            if stripped:
                self._title = stripped
        if self._skip:
            return
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)

    def result(self) -> tuple[str | None, str]:
        text = " ".join(self._parts)
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = re.sub(r"\s*\n\s*", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return self._title, text.strip()


def _strip_html(markup: str) -> tuple[str | None, str]:
    parser = _HtmlToText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception as exc:  # pragma: no cover - malformed markup
        log.debug("html parse fell back to raw text: %s", exc)
        return None, markup.strip()
    return parser.result()


def _private_target_reason(host: str | None, *, resolve: bool) -> str | None:
    """Why a host must not be fetched, or None when it is public.

    ``web.fetch`` is auto-allowed. Without this guard, a page or prompt could
    ask it to read the loopback Core API (``/api/audit``, ``/api/approvals``),
    a router, or a cloud metadata endpoint and put the response in model
    context. Names are resolved too, so ``127.0.0.1.nip.io`` is refused.
    """
    name = str(host or "").strip().strip("[]").split("%", 1)[0].lower()
    if not name:
        return "URL has no host"
    if name in {"localhost", "localhost.localdomain"} or name.endswith(".localhost"):
        return f"refused: '{name}' is a loopback name"
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            return f"refused: '{name}' is a loopback/private/link-local address"
        return None
    if not resolve:
        return None
    try:
        infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return f"refused: host '{name}' could not be resolved"
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if not addr.is_global:
            return (f"refused: '{name}' resolves to the non-public address "
                    f"'{addr}'")
    return None


def _check_url(url: str, *, allow_private: bool = False,
               resolve: bool = False) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return f"invalid URL: {exc}"
    if parsed.scheme not in ("http", "https"):
        return f"unsupported URL scheme '{parsed.scheme or '(none)'}'; only http/https"
    if not parsed.netloc:
        return "URL has no host"
    if allow_private:
        return None
    reason = _private_target_reason(parsed.hostname, resolve=resolve)
    return reason


@register(["fetch", "fetch_url", "http_get"])
class WebFetchTool(BaseTool):
    spec = ToolSpec(
        name="web.fetch",
        summary="Fetch a URL and return its title and readable text.",
        category="web",
        side_effect=SideEffect.NETWORK,
        needs_network=True,
        cheap=False,
        params=(
            ToolParam("url", "string", "Absolute http(s) URL. Required.", required=True),
            ToolParam("max_bytes", "integer", "Response size cap in bytes.",
                      default=500000),
            ToolParam("max_chars", "integer", "Text length cap in characters.",
                      default=20000),
            ToolParam("timeout", "number", "Request timeout in seconds.", default=20.0),
        ),
        returns="object with final url, status, title, text and a truncation flag",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        url = str(kwargs["url"]).strip()
        security = getattr(getattr(ctx, "config", None), "security", None)
        allow_private = bool(getattr(security, "allow_private_fetch", False))
        bad = _check_url(url, allow_private=allow_private)
        if bad:
            return ToolResult(ok=False, error=bad, certainty="settled")
        if not allow_private:
            # Resolve once so a public name rebinding to loopback/private is
            # refused as well. Fail closed when the name cannot be resolved or
            # the resolver hangs.
            try:
                bad = await asyncio.wait_for(
                    asyncio.to_thread(_check_url, url, allow_private=False, resolve=True),
                    timeout=5.0)
            except asyncio.TimeoutError:
                return ToolResult(ok=False, error="refused: DNS resolution timed out",
                                  certainty="settled")
            if bad:
                return ToolResult(ok=False, error=bad, certainty="settled")
        try:
            try:
                max_bytes = int(kwargs.get("max_bytes") or 500000)
                max_chars = int(kwargs.get("max_chars") or 20000)
                timeout = float(kwargs.get("timeout") or 20.0)
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="max_bytes/max_chars/timeout must be numeric")
            max_bytes = max(1024, min(max_bytes, 8 * 1024 * 1024))
            max_chars = max(200, min(max_chars, 200000))
            timeout = max(1.0, min(timeout, 120.0))

            headers = {"User-Agent": _USER_AGENT,
                       "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"}
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    status = resp.status_code
                    final_url = str(resp.url)
                    ctype = resp.headers.get("content-type", "")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= max_bytes:
                            break
                    raw = b"".join(chunks)[:max_bytes]
                    size_truncated = total >= max_bytes

            charset = "utf-8"
            match = re.search(r"charset=([\w\-]+)", ctype, re.IGNORECASE)
            if match:
                charset = match.group(1)
            try:
                decoded = raw.decode(charset, "replace")
            except LookupError:
                decoded = raw.decode("utf-8", "replace")

            is_html = "html" in ctype.lower() or decoded.lstrip()[:200].lower().startswith("<!doctype html")
            if is_html:
                title, text = _strip_html(decoded)
            else:
                title, text = None, decoded.strip()

            text_truncated = len(text) > max_chars
            if status >= 400:
                return ToolResult(
                    ok=False,
                    error=f"HTTP {status} fetching {final_url}",
                    data={"url": final_url, "status": status, "content_type": ctype,
                          "title": title, "text": text[:max_chars]},
                )
            return ToolResult(ok=True, data={
                "url": final_url, "status": status, "content_type": ctype,
                "title": title, "text": text[:max_chars],
                "truncated": size_truncated or text_truncated,
            })
        except httpx.TimeoutException:
            return ToolResult(ok=False, error=f"timeout fetching {url}")
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"request failed: {exc}")
        except Exception as exc:
            log.warning("web.fetch failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["search", "web_search", "web_lookup"])
class WebSearchTool(BaseTool):
    spec = ToolSpec(
        name="web.search",
        summary="Search the web via a configured SearXNG provider.",
        category="web",
        side_effect=SideEffect.NETWORK,
        needs_network=True,
        cheap=False,
        params=(
            ToolParam("query", "string", "Search query. Required.", required=True),
            ToolParam("top_k", "integer", "Maximum results.", default=8),
            ToolParam("categories", "string", "SearXNG categories.", default="general"),
        ),
        returns="object with ranked results (title, url, snippet)",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        lookup = ctx.secrets
        base = (lookup("searx_url") or "").strip() if callable(lookup) else ""
        if not base:
            return ToolResult(
                ok=False,
                error=("no search provider configured: set the 'searx_url' secret "
                       "(JARVIS_SECRET_SEARX_URL or data/secrets.json) to a SearXNG "
                       "instance, e.g. http://127.0.0.1:8888"),
            )
        # The owner configures the search provider; a local SearXNG on
        # 127.0.0.1:8888 is the documented setup, so this path may be private.
        if _check_url(base, allow_private=True):
            return ToolResult(ok=False,
                              error=f"configured 'searx_url' is not a valid http(s) URL: {base!r}")
        try:
            query = str(kwargs["query"]).strip()
            categories = str(kwargs.get("categories") or "general")
            try:
                top_k = int(kwargs.get("top_k") or 8)
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="top_k must be an integer")
            top_k = max(1, min(top_k, 50))

            endpoint = base.rstrip("/") + "/search"
            headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(endpoint, params={
                    "q": query, "format": "json", "categories": categories,
                }, headers=headers)
            if resp.status_code >= 400:
                return ToolResult(
                    ok=False,
                    error=(f"search provider returned HTTP {resp.status_code}; ensure the "
                           f"SearXNG instance has the JSON output format enabled"),
                )
            try:
                payload = resp.json()
            except ValueError:
                return ToolResult(
                    ok=False,
                    error="search provider did not return JSON (enable the JSON format)",
                )

            results = []
            for item in payload.get("results", [])[:top_k]:
                results.append({
                    "title": item.get("title") or "",
                    "url": item.get("url") or "",
                    "snippet": item.get("content") or "",
                    "engine": item.get("engine"),
                    "score": item.get("score"),
                })
            return ToolResult(ok=True, data={
                "provider": "searx", "query": query, "count": len(results),
                "results": results,
            })
        except httpx.TimeoutException:
            return ToolResult(ok=False, error="search provider timed out")
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"search request failed: {exc}")
        except Exception as exc:
            log.warning("web.search failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
