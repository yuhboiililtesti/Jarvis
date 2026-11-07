"""OpenAI-compatible serving endpoints for JARVIS.

JARVIS can act as a drop-in OpenAI-compatible backend: ``GET /v1/models``,
``POST /v1/chat/completions`` (streaming and non-streaming) and
``POST /v1/embeddings``. This lets any OpenAI-protocol client (including the
owner's "Personal AI" frontend, code editors, or a future phone app) talk to
JARVIS exactly as it would to a commercial provider, while the model stays
local and replaceable — the same design rule as the Personal AI core.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..llm.providers import ChatMessage
from ..logging_setup import get_logger

log = get_logger("jarvis.api.openai")

__all__ = ["build_openai_router"]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _to_messages(messages: Any) -> list[ChatMessage]:
    """Convert OpenAI ``messages`` (text, and text parts of content arrays)."""
    out: list[ChatMessage] = []
    for m in messages or []:
        role = str(m.get("role") or "user")
        content = m.get("content")
        if isinstance(content, list):
            parts = [str(p.get("text", "")) for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            content = "\n".join(p for p in parts if p)
        out.append(ChatMessage(role, str(content or "")))
    return out


def _openai_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Normalise provider tool-call output into OpenAI's shape."""
    out: list[dict[str, Any]] = []
    for i, tc in enumerate(raw or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name") or ""
        args = fn.get("arguments") or tc.get("arguments") or {}
        if isinstance(args, (dict, list)):
            args = json.dumps(args)
        out.append({
            "id": f"call_{i}_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": str(name), "arguments": str(args)},
        })
    return out


def build_openai_router(kernel: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def models() -> dict[str, Any]:
        names: list[str] = []
        try:
            primary = kernel.router.primary
            if primary is not None:
                names = await primary.list_models()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("openai /v1/models failed: %s", exc)
        return {"object": "list",
                "data": [{"id": n, "object": "model", "created": 0,
                          "owned_by": "jarvis"} for n in names]}

    @router.post("/v1/embeddings")
    async def embeddings(payload: dict[str, Any]) -> dict[str, Any]:
        inp = payload.get("input")
        model = payload.get("model") or kernel.config.llm.embed_model
        if isinstance(inp, str):
            texts = [inp]
        elif isinstance(inp, list):
            texts = [str(x) for x in inp]
        else:
            raise HTTPException(status_code=400,
                                detail="input must be a string or list of strings")
        data: list[dict[str, Any]] = []
        for text in texts:
            vec = await kernel.router.embed(text)
            if not vec:
                raise HTTPException(
                    status_code=502,
                    detail=f"embedding unavailable for model '{model}' "
                           "(is it installed and reachable?)")
            data.append({"object": "embedding", "index": len(data),
                         "embedding": vec})
        return {"object": "list", "data": data, "model": model}

    @router.post("/v1/chat/completions")
    async def chat_completions(payload: dict[str, Any]) -> Any:
        messages = _to_messages(payload.get("messages"))
        if not messages:
            raise HTTPException(status_code=400, detail="messages is required")

        model = payload.get("model") or kernel.config.llm.model
        stream = bool(payload.get("stream", False))
        temperature = payload.get("temperature")
        max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
        tools = payload.get("tools")

        provider = kernel.router.primary
        if provider is None:
            raise HTTPException(status_code=503, detail="no LLM provider configured")

        created = int(time.time())
        cid = _new_id("chatcmpl")

        async def events():
            async for ev in provider.chat(messages, model=model, tools=tools or None,
                                          temperature=temperature,
                                          num_predict=max_tokens):
                yield ev

        if stream:
            async def gen():
                role_sent = False
                async for ev in events():
                    if ev.kind == "token":
                        delta: dict[str, Any] = {"content": ev.text}
                        if not role_sent:
                            delta["role"] = "assistant"
                            role_sent = True
                        chunk = {"id": cid, "object": "chat.completion.chunk",
                                 "created": created, "model": model,
                                 "choices": [{"index": 0, "delta": delta,
                                              "finish_reason": None}]}
                        yield f"data: {json.dumps(chunk, default=str)}\n\n"
                    elif ev.kind == "tool_calls":
                        chunk = {"id": cid, "object": "chat.completion.chunk",
                                 "created": created, "model": model,
                                 "choices": [{"index": 0,
                                              "delta": {"tool_calls": _openai_tool_calls(ev.tool_calls)},
                                              "finish_reason": None}]}
                        yield f"data: {json.dumps(chunk, default=str)}\n\n"
                    elif ev.kind == "error":
                        chunk = {"id": cid, "object": "chat.completion.chunk",
                                 "created": created, "model": model,
                                 "choices": [{"index": 0, "delta": {},
                                              "finish_reason": "error"}]}
                        yield f"data: {json.dumps(chunk, default=str)}\n\n"
                final = {"id": cid, "object": "chat.completion.chunk",
                         "created": created, "model": model,
                         "choices": [{"index": 0, "delta": {},
                                      "finish_reason": "stop"}]}
                yield f"data: {json.dumps(final)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")

        # non-streaming
        text_parts: list[str] = []
        raw_tool_calls: list[dict[str, Any]] = []
        error: str | None = None
        meta: dict[str, Any] = {}
        async for ev in events():
            if ev.kind == "token":
                text_parts.append(ev.text)
            elif ev.kind == "tool_calls":
                raw_tool_calls.extend(ev.tool_calls)
            elif ev.kind == "meta":
                meta.update(ev.meta)
            elif ev.kind == "error":
                error = ev.error

        if error and not text_parts and not raw_tool_calls:
            raise HTTPException(status_code=502, detail=error)

        content = "".join(text_parts) or None
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if raw_tool_calls:
            message["tool_calls"] = _openai_tool_calls(raw_tool_calls)
        finish = "tool_calls" if raw_tool_calls else "stop"

        usage: dict[str, Any] = {}
        if meta.get("eval_count") is not None:
            usage = {
                "completion_tokens": meta.get("eval_count"),
                "prompt_tokens": meta.get("prompt_eval_count"),
                "total_tokens": (meta.get("eval_count") or 0)
                + (meta.get("prompt_eval_count") or 0),
            }
        return {"id": cid, "object": "chat.completion", "created": created,
                "model": model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": finish}],
                "usage": usage}

    return router
