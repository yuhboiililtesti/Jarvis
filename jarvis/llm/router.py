"""Model router: eligible provider selection, health and fallback.

Fallback is only ever to another *eligible* provider (same privacy class). A
local-only configuration never silently reaches a remote endpoint. Every failure
is surfaced as a typed event, never swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .providers import (OllamaProvider, OpenAICompatProvider, Provider,
                        ProviderError, StreamEvent, UnavailableError)
from .frontier import build_frontier
from ..logging_setup import get_logger

log = get_logger("jarvis.llm.router")

__all__ = ["ModelRouter"]


@dataclass
class Route:
    provider: Provider
    model: str
    reason: str = "primary"


class ModelRouter:
    def __init__(self, config: Any, secrets: Any = None) -> None:
        self.config = config
        self.secrets = secrets
        self._primary: Provider | None = None
        self._fallbacks: list[Provider] = []
        self._frontier: Provider | None = None
        self._build()

    def _frontier_key(self) -> str:
        llm = self.config.llm
        if not llm.frontier_key_secret:
            return ""
        if self.secrets is not None:
            return self.secrets.get(llm.frontier_key_secret) or ""
        import os
        return os.environ.get(f"JARVIS_SECRET_{llm.frontier_key_secret.upper()}", "")

    def _build(self) -> None:
        llm = self.config.llm
        if llm.provider == "ollama":
            self._primary = OllamaProvider(
                llm.base_url, default_model=llm.model, timeout=llm.request_timeout_s,
                keep_alive=llm.keep_alive, embed_model=llm.embed_model)
        elif llm.provider == "openai_compat":
            self._primary = OpenAICompatProvider(
                llm.base_url, default_model=llm.model, timeout=llm.request_timeout_s)
        else:
            self._primary = None
            log.warning("no LLM provider configured (provider=%s)", llm.provider)

        for entry in llm.fallback:
            url, _, model = entry.partition("|")
            self._fallbacks.append(OllamaProvider(
                url.strip(), default_model=model.strip() or llm.model,
                timeout=llm.request_timeout_s, embed_model=llm.embed_model))

        # Optional frontier tier (remote, high-capability). Built only when a
        # provider and key are both configured; otherwise it stays None and the
        # tier is simply unavailable (never a silent remote call).
        if llm.frontier_provider:
            key = self._frontier_key()
            if key:
                try:
                    self._frontier = build_frontier(
                        llm.frontier_provider, key, llm.frontier_model,
                        timeout=llm.request_timeout_s)
                    log.info("frontier tier ready: %s/%s",
                             llm.frontier_provider, llm.frontier_model or "default")
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("frontier provider could not build: %s", exc)
            else:
                log.warning("frontier provider '%s' configured but no API key "
                            "found (secret '%s'); tier disabled",
                            llm.frontier_provider, llm.frontier_key_secret)

    @property
    def primary(self) -> Provider | None:
        return self._primary

    @property
    def frontier(self) -> Provider | None:
        return self._frontier

    async def health(self) -> dict[str, Any]:
        if not self._primary:
            return {"ok": False, "error": "no provider configured"}
        h = await self._primary.health()
        if self._frontier is not None:
            h["frontier"] = await self._frontier.health()
        return h

    async def model_available(self, model: str) -> bool:
        if not self._primary:
            return False
        models = await self._primary.list_models()
        return any(m == model or m.split(":")[0] == model for m in models)

    def route(self, *, want: str = "primary") -> Route:
        llm = self.config.llm
        if want == "frontier":
            if not self._frontier:
                raise UnavailableError(
                    "frontier tier not configured (provider/key missing)")
            return Route(self._frontier,
                         llm.frontier_model or self._frontier.default_model,
                         "frontier")
        if not self._primary:
            raise UnavailableError("no LLM provider configured")
        if want == "fast" and llm.fast_model:
            return Route(self._primary, llm.fast_model, "fast model")
        return Route(self._primary, llm.model or self._primary.default_model, "primary")

    def _fallback_chain(self, route: Route) -> list[Provider]:
        """Eligible fallbacks, preserving privacy class (never local -> remote)."""
        if self._frontier is not None and route.provider is self._frontier:
            # A failed remote call may fall back to local providers (safe).
            chain: list[Provider] = []
            if self._primary is not None:
                chain.append(self._primary)
            chain.extend(self._fallbacks)
            return chain
        return list(self._fallbacks)

    async def chat(self, messages, *, want: str = "primary", **kw):
        """Stream with an eligible fallback attempt on connection failure."""
        route = self.route(want=want)
        got_any = False
        async for ev in route.provider.chat(messages, model=route.model, **kw):
            if ev.kind == "error" and not got_any:
                for fb in self._fallback_chain(route):
                    log.warning("provider '%s' failed (%s); trying %s",
                                route.provider.name, ev.error, fb.name)
                    async for ev2 in fb.chat(messages, model=fb.default_model, **kw):
                        if ev2.kind == "error":
                            yield ev2
                            return
                        got_any = True
                        yield ev2
                    return
                yield ev
                return
            got_any = got_any or ev.kind in ("token", "tool_calls")
            yield ev

    async def embed(self, text: str) -> list[float]:
        if not self._primary:
            return []
        return await self._primary.embed(text)
