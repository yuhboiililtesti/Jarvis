"""LLM provider abstraction.

A provider yields *typed* stream events so the caller can distinguish real text
from tool calls and from errors — never a silent empty string. Ollama is the
primary local provider; an OpenAI-compatible provider covers llama.cpp,
LM Studio, vLLM, etc. There is no cloud dependency and no token cap imposed
here (budgets are the owner's, enforced in the router).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

import httpx

from ..logging_setup import get_logger

log = get_logger("jarvis.llm")

__all__ = [
    "ChatMessage", "StreamEvent", "Provider", "OllamaProvider",
    "OpenAICompatProvider", "ProviderError", "UnavailableError",
]


class ProviderError(RuntimeError):
    """Provider failed in a way the caller must surface (typed, not silent)."""


class UnavailableError(ProviderError):
    """Provider is not reachable / not configured."""


@dataclass
class ChatMessage:
    role: str          # system | user | assistant | tool
    content: str = ""
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d


@dataclass
class StreamEvent:
    kind: str                       # token | tool_call | done | error | meta
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    async def health(self) -> dict[str, Any]: ...
    async def list_models(self) -> list[str]: ...
    async def chat(self, messages: list[ChatMessage], *, model: str | None = None,
                   tools: list[dict[str, Any]] | None = None,
                   temperature: float | None = None,
                   num_ctx: int | None = None) -> AsyncIterator[StreamEvent]: ...
    async def embed(self, text: str, *, model: str | None = None) -> list[float]: ...


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, *, default_model: str = "qwen2.5:7b-instruct",
                 timeout: float = 180.0, keep_alive: str = "30m",
                 embed_model: str = "nomic-embed-text") -> None:
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.embed_model = embed_model
        self.timeout = timeout
        self.keep_alive = keep_alive

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self.base_url}/api/tags")
                r.raise_for_status()
                models = [m["name"] for m in r.json().get("models", [])]
            return {"ok": True, "provider": self.name, "models": models}
        except Exception as exc:
            return {"ok": False, "provider": self.name, "error": str(exc)}

    async def list_models(self) -> list[str]:
        h = await self.health()
        return h.get("models", []) if h.get("ok") else []

    async def pull(self, model: str) -> AsyncIterator[dict[str, Any]]:
        """Stream progress of a model pull (for the setup UI)."""
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", f"{self.base_url}/api/pull",
                                json={"name": model, "stream": True}) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if line.strip():
                        yield json.loads(line)

    async def chat(self, messages: list[ChatMessage], *, model: str | None = None,
                   tools: list[dict[str, Any]] | None = None,
                   temperature: float | None = None,
                   num_ctx: int | None = None,
                   num_predict: int | None = None) -> AsyncIterator[StreamEvent]:
        model = model or self.default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {},
        }
        if temperature is not None:
            payload["options"]["temperature"] = temperature
        if num_ctx is not None:
            payload["options"]["num_ctx"] = num_ctx
        if num_predict is not None:
            payload["options"]["num_predict"] = num_predict
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                async with c.stream("POST", f"{self.base_url}/api/chat", json=payload) as r:
                    if r.status_code >= 400:
                        body = (await r.aread()).decode("utf-8", "replace")
                        yield StreamEvent("error", error=f"ollama {r.status_code}: {body[:300]}")
                        return
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = obj.get("message") or {}
                        if msg.get("content"):
                            yield StreamEvent("token", text=msg["content"])
                        if msg.get("tool_calls"):
                            yield StreamEvent("tool_calls", tool_calls=msg["tool_calls"])
                        if obj.get("done"):
                            yield StreamEvent("meta", meta={
                                "eval_count": obj.get("eval_count"),
                                "prompt_eval_count": obj.get("prompt_eval_count"),
                                "total_duration": obj.get("total_duration"),
                                "model": model,
                            })
                            yield StreamEvent("done")
                            return
        except httpx.ConnectError as exc:
            yield StreamEvent("error", error=f"ollama unreachable at {self.base_url}: {exc}")
        except httpx.TimeoutException:
            yield StreamEvent("error", error="provider timeout")
        except Exception as exc:  # pragma: no cover
            yield StreamEvent("error", error=f"{type(exc).__name__}: {exc}")

    async def embed(self, text: str, *, model: str | None = None) -> list[float]:
        model = model or self.embed_model
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(f"{self.base_url}/api/embeddings",
                                 json={"model": model, "prompt": text})
                r.raise_for_status()
                return r.json().get("embedding", [])
        except Exception as exc:
            log.debug("embedding unavailable: %s", exc)
            return []


class OpenAICompatProvider:
    """Minimal client for any /v1/chat/completions server (llama.cpp, vLLM...)."""

    name = "openai_compat"

    def __init__(self, base_url: str, *, api_key: str = "local",
                 default_model: str = "local-model", timeout: float = 180.0,
                 embed_model: str = "",
                 transport: Any | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.timeout = timeout
        self.embed_model = embed_model
        self._transport = transport  # injectable for tests

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def health(self) -> dict[str, Any]:
        try:
            async with self._client(5.0) as c:
                r = await c.get(f"{self.base_url}/models", headers=self._headers())
                r.raise_for_status()
                return {"ok": True, "provider": self.name,
                        "models": [m["id"] for m in r.json().get("data", [])]}
        except Exception as exc:
            return {"ok": False, "provider": self.name, "error": str(exc)}

    async def list_models(self) -> list[str]:
        h = await self.health()
        return h.get("models", []) if h.get("ok") else []

    async def chat(self, messages: list[ChatMessage], *, model: str | None = None,
                   tools: list[dict[str, Any]] | None = None,
                   temperature: float | None = None,
                   num_ctx: int | None = None,
                   num_predict: int | None = None) -> AsyncIterator[StreamEvent]:
        model = model or self.default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if num_predict is not None:
            payload["max_tokens"] = num_predict
        if tools:
            payload["tools"] = tools
        try:
            async with self._client(self.timeout) as c:
                async with c.stream("POST", f"{self.base_url}/chat/completions",
                                    json=payload, headers=self._headers()) as r:
                    if r.status_code >= 400:
                        body = (await r.aread()).decode("utf-8", "replace")
                        yield StreamEvent("error", error=f"http {r.status_code}: {body[:300]}")
                        return
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            yield StreamEvent("done")
                            return
                        try:
                            obj = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                        if delta.get("content"):
                            yield StreamEvent("token", text=delta["content"])
                        if delta.get("tool_calls"):
                            yield StreamEvent("tool_calls", tool_calls=delta["tool_calls"])
            yield StreamEvent("done")
        except Exception as exc:
            yield StreamEvent("error", error=f"{type(exc).__name__}: {exc}")

    async def embed(self, text: str, *, model: str | None = None) -> list[float]:
        if not self.embed_model:
            return []
        try:
            async with self._client(60.0) as c:
                r = await c.post(f"{self.base_url}/embeddings",
                                 json={"model": model or self.embed_model, "input": text},
                                 headers=self._headers())
                r.raise_for_status()
                return r.json()["data"][0]["embedding"]
        except Exception:
            return []
