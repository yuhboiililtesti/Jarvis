"""Frontier LLM providers: native clients for remote, high-capability models.

Anthropic (Messages API) and Gemini (generateContent) are non-streaming under
the hood — the whole response is fetched, then re-emitted as typed
:class:`StreamEvent` tokens so the caller's streaming contract is unchanged.
Groq is OpenAI-compatible and reuses :class:`OpenAICompatProvider`.

Every provider converts JARVIS's internal tool-calling shape (OpenAI
``tool_calls``) to the provider's native format and back, so the agent loop is
unchanged whether the model is local or remote. A missing API key is reported
as an :class:`UnavailableError`, never a silent empty stream.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .providers import (ChatMessage, OpenAICompatProvider, StreamEvent,
                        UnavailableError)
from ..logging_setup import get_logger

log = get_logger("jarvis.llm.frontier")

__all__ = ["GroqProvider", "AnthropicProvider", "GeminiProvider", "build_frontier"]


def _parse_json(s: Any) -> Any:
    """Return a dict for a JSON string; pass dicts through."""
    if isinstance(s, str):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return {}
    return s if isinstance(s, dict) else {}


def _fn(tc: dict[str, Any]) -> dict[str, Any]:
    return tc.get("function") or {}


# --------------------------------------------------------------------------- #
# Groq — OpenAI-compatible, so it is a thin subclass.
# --------------------------------------------------------------------------- #
class GroqProvider(OpenAICompatProvider):
    name = "groq"

    def __init__(self, api_key: str, *, default_model: str = "llama-3.3-70b-versatile",
                 timeout: float = 120.0, transport: Any | None = None) -> None:
        super().__init__("https://api.groq.com/openai/v1", api_key=api_key,
                         default_model=default_model, timeout=timeout,
                         embed_model="", transport=transport)


# --------------------------------------------------------------------------- #
# Anthropic — native Messages API.
# --------------------------------------------------------------------------- #
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, *,
                 default_model: str = "claude-3-5-sonnet-20241022",
                 timeout: float = 120.0, transport: Any | None = None,
                 base_url: str = "https://api.anthropic.com") -> None:
        self.api_key = api_key
        self.default_model = default_model
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"}

    async def health(self) -> dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "provider": self.name, "error": "no api key configured"}
        try:
            async with self._client(10.0) as c:
                r = await c.get(f"{self.base_url}/v1/models", headers=self._headers())
                r.raise_for_status()
                return {"ok": True, "provider": self.name,
                        "models": [m["id"] for m in r.json().get("data", [])]}
        except Exception as exc:
            return {"ok": False, "provider": self.name, "error": str(exc)}

    async def list_models(self) -> list[str]:
        h = await self.health()
        return h.get("models", []) if h.get("ok") else []

    # -- conversion helpers (module-level functions for testability) ------
    def _messages(self, messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
        system = ""
        out: list[dict[str, Any]] = []
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            if m.role == "system":
                system = (system + "\n" + m.content).strip() if system else m.content
                i += 1
                continue
            if m.role == "tool":
                # group consecutive tool results into one user turn
                blocks: list[dict[str, Any]] = []
                while i < n and messages[i].role == "tool":
                    tm = messages[i]
                    blocks.append({"type": "tool_result",
                                   "tool_use_id": tm.tool_call_id or "",
                                   "content": tm.content})
                    i += 1
                out.append({"role": "user", "content": blocks})
                continue
            if m.role == "assistant":
                if m.tool_calls:
                    blocks = []
                    if m.content:
                        blocks.append({"type": "text", "text": m.content})
                    for tc in m.tool_calls:
                        fn = _fn(tc)
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id") or f"call_{len(blocks)}",
                            "name": fn.get("name") or tc.get("name") or "",
                            "input": _parse_json(fn.get("arguments") or tc.get("arguments") or "{}"),
                        })
                    out.append({"role": "assistant", "content": blocks})
                else:
                    out.append({"role": "assistant", "content": m.content})
                i += 1
                continue
            out.append({"role": "user", "content": m.content})
            i += 1
        return system, out

    async def chat(self, messages: list[ChatMessage], *, model: str | None = None,
                   tools: list[dict[str, Any]] | None = None,
                   temperature: float | None = None,
                   num_ctx: int | None = None,
                   num_predict: int | None = None) -> AsyncIterator[StreamEvent]:
        if not self.api_key:
            yield StreamEvent("error", error="anthropic: no api key configured")
            return
        model = model or self.default_model
        system, msgs = self._messages(messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "max_tokens": num_predict or 1024,
        }
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = [
                {"name": (_fn(t).get("name") or t.get("name") or ""),
                 "description": _fn(t).get("description", ""),
                 "input_schema": _fn(t).get("parameters") or
                                {"type": "object", "properties": {}}}
                for t in tools]
        try:
            async with self._client(self.timeout) as c:
                r = await c.post(f"{self.base_url}/v1/messages",
                                 json=payload, headers=self._headers())
                if r.status_code >= 400:
                    body = r.text[:300]
                    yield StreamEvent("error", error=f"anthropic {r.status_code}: {body}")
                    return
                data = r.json()
        except httpx.ConnectError as exc:
            yield StreamEvent("error", error=f"anthropic unreachable: {exc}")
            return
        except Exception as exc:
            yield StreamEvent("error", error=f"{type(exc).__name__}: {exc}")
            return

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {"name": block.get("name"),
                                 "arguments": json.dumps(block.get("input", {}))},
                })
        if text_parts:
            yield StreamEvent("token", text="".join(text_parts))
        if tool_calls:
            yield StreamEvent("tool_calls", tool_calls=tool_calls)
        yield StreamEvent("meta", meta={"model": model,
                                        "stop_reason": data.get("stop_reason"),
                                        "usage": data.get("usage")})
        yield StreamEvent("done")

    async def embed(self, text: str, *, model: str | None = None) -> list[float]:
        return []  # chat-only; embeddings stay local


# --------------------------------------------------------------------------- #
# Gemini — native generateContent API.
# --------------------------------------------------------------------------- #
def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a JSON-Schema dict to Gemini's OpenAPI-style (uppercase types)."""
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.upper()
        elif k in ("properties", "definitions") and isinstance(v, dict):
            out[k] = {name: _gemini_schema(sub) for name, sub in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    return out


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, *,
                 default_model: str = "gemini-2.0-flash",
                 timeout: float = 120.0, transport: Any | None = None,
                 base_url: str = "https://generativelanguage.googleapis.com") -> None:
        self.api_key = api_key
        self.default_model = default_model
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def health(self) -> dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "provider": self.name, "error": "no api key configured"}
        try:
            async with self._client(10.0) as c:
                r = await c.get(f"{self.base_url}/v1beta/models",
                                params={"key": self.api_key})
                r.raise_for_status()
                return {"ok": True, "provider": self.name,
                        "models": [m["name"] for m in r.json().get("models", [])]}
        except Exception as exc:
            return {"ok": False, "provider": self.name, "error": str(exc)}

    async def list_models(self) -> list[str]:
        h = await self.health()
        return h.get("models", []) if h.get("ok") else []

    def _messages(self, messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
        system = ""
        out: list[dict[str, Any]] = []
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            if m.role == "system":
                system = (system + "\n" + m.content).strip() if system else m.content
                i += 1
                continue
            if m.role == "tool":
                parts: list[dict[str, Any]] = []
                while i < n and messages[i].role == "tool":
                    tm = messages[i]
                    parts.append({"functionResponse": {
                        "name": tm.name or "",
                        "response": {"result": tm.content},
                    }})
                    i += 1
                out.append({"role": "user", "parts": parts})
                continue
            if m.role == "assistant":
                if m.tool_calls:
                    parts = []
                    if m.content:
                        parts.append({"text": m.content})
                    for tc in m.tool_calls:
                        fn = _fn(tc)
                        parts.append({"functionCall": {
                            "name": fn.get("name") or tc.get("name") or "",
                            "args": _parse_json(fn.get("arguments") or tc.get("arguments") or "{}"),
                        }})
                    out.append({"role": "model", "parts": parts})
                else:
                    out.append({"role": "model", "parts": [{"text": m.content}]})
                i += 1
                continue
            out.append({"role": "user", "parts": [{"text": m.content}]})
            i += 1
        return system, out

    async def chat(self, messages: list[ChatMessage], *, model: str | None = None,
                   tools: list[dict[str, Any]] | None = None,
                   temperature: float | None = None,
                   num_ctx: int | None = None,
                   num_predict: int | None = None) -> AsyncIterator[StreamEvent]:
        if not self.api_key:
            yield StreamEvent("error", error="gemini: no api key configured")
            return
        model = model or self.default_model
        system, contents = self._messages(messages)
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if temperature is not None:
            payload["generationConfig"] = {"temperature": temperature}
        if tools:
            decls = [
                {"name": (_fn(t).get("name") or t.get("name") or ""),
                 "description": _fn(t).get("description", ""),
                 "parameters": _gemini_schema(
                     _fn(t).get("parameters") or {"type": "object", "properties": {}})}
                for t in tools]
            payload["tools"] = [{"functionDeclarations": decls}]
        try:
            async with self._client(self.timeout) as c:
                r = await c.post(
                    f"{self.base_url}/v1beta/models/{model}:generateContent",
                    params={"key": self.api_key}, json=payload)
                if r.status_code >= 400:
                    body = r.text[:300]
                    yield StreamEvent("error", error=f"gemini {r.status_code}: {body}")
                    return
                data = r.json()
        except httpx.ConnectError as exc:
            yield StreamEvent("error", error=f"gemini unreachable: {exc}")
            return
        except Exception as exc:
            yield StreamEvent("error", error=f"{type(exc).__name__}: {exc}")
            return

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for cand in data.get("candidates", []):
            for part in (cand.get("content") or {}).get("parts", []):
                if "text" in part:
                    text_parts.append(part["text"])
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append({
                        "id": f"call_{len(tool_calls)}",
                        "type": "function",
                        "function": {"name": fc.get("name"),
                                     "arguments": json.dumps(fc.get("args", {}))},
                    })
        if text_parts:
            yield StreamEvent("token", text="".join(text_parts))
        if tool_calls:
            yield StreamEvent("tool_calls", tool_calls=tool_calls)
        yield StreamEvent("meta", meta={"model": model,
                                        "usage": data.get("usageMetadata")})
        yield StreamEvent("done")

    async def embed(self, text: str, *, model: str | None = None) -> list[float]:
        return []  # chat-only; embeddings stay local


def build_frontier(kind: str, api_key: str, model: str, *,
                   timeout: float = 120.0, transport: Any | None = None):
    """Construct a frontier provider from a kind string; raises on unknown kind."""
    k = (kind or "").lower()
    if k == "anthropic":
        return AnthropicProvider(api_key, default_model=model, timeout=timeout,
                                 transport=transport)
    if k == "gemini":
        return GeminiProvider(api_key, default_model=model, timeout=timeout,
                              transport=transport)
    if k == "groq":
        return GroqProvider(api_key, default_model=model, timeout=timeout,
                            transport=transport)
    raise UnavailableError(f"unknown frontier provider {kind!r}")
