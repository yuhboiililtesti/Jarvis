"""Frontier LLM providers and tiered router — mocked-HTTP tests."""

from __future__ import annotations

import json

import httpx
import pytest

from jarvis.config import Config
from jarvis.llm import (AnthropicProvider, GeminiProvider, GroqProvider,
                        build_frontier)
from jarvis.llm.providers import ChatMessage, UnavailableError
from jarvis.llm.router import ModelRouter, Route


async def _events(provider, messages, **kw):
    return [ev async for ev in provider.chat(messages, **kw)]


# ------------------------------------------------------------------- Groq ---

GROQ_SSE = (
    'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
    'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1","type":"function",'
    '"function":{"name":"system.info","arguments":"{}"}}]}}]}\n\n'
    'data: [DONE]\n\n'
)


async def test_groq_chat_text_and_tool_calls():
    def handler(request):
        return httpx.Response(200, content=GROQ_SSE,
                              headers={"content-type": "text/event-stream"})
    p = GroqProvider("test-key", transport=httpx.MockTransport(handler))
    msgs = [ChatMessage("user", "hi")]
    events = await _events(p, msgs, tools=[{"type": "function", "function": {"name": "system.info"}}])
    kinds = [e.kind for e in events]
    assert "token" in kinds and "tool_calls" in kinds and "done" in kinds
    text = "".join(e.text for e in events if e.kind == "token")
    assert text == "Hello"
    calls = [e.tool_calls for e in events if e.kind == "tool_calls"]
    assert calls and calls[0][0]["function"]["name"] == "system.info"


# --------------------------------------------------------------- Anthropic ---

def _anthropic_body():
    return {
        "content": [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "id": "toolu_1", "name": "system.info",
             "input": {"foo": "bar"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


async def test_anthropic_chat_text_and_tool_use():
    def handler(request):
        return httpx.Response(200, json=_anthropic_body())
    p = AnthropicProvider("test-key", transport=httpx.MockTransport(handler))
    events = await _events(p, [ChatMessage("user", "hi")], tools=[
        {"type": "function", "function": {"name": "system.info"}}])
    text = "".join(e.text for e in events if e.kind == "token")
    assert text == "Hello"
    calls = [e.tool_calls for e in events if e.kind == "tool_calls"]
    assert calls and calls[0][0]["function"]["name"] == "system.info"
    # arguments are re-serialised from Anthropic's `input` object
    assert json.loads(calls[0][0]["function"]["arguments"]) == {"foo": "bar"}


def test_anthropic_message_conversion():
    p = AnthropicProvider("k")
    msgs = [
        ChatMessage("system", "be terse"),
        ChatMessage("user", "run it"),
        ChatMessage("assistant", "ok", tool_calls=[
            {"id": "call_1", "function": {"name": "system.info", "arguments": "{}"}}]),
        ChatMessage("tool", "1 cpu", name="system.info", tool_call_id="call_1"),
    ]
    system, body = p._messages(msgs)
    assert system == "be terse"
    # assistant with tool_use, then a user message with tool_result
    assert body[0] == {"role": "user", "content": "run it"}
    assert body[1]["role"] == "assistant"
    assert body[1]["content"][0]["type"] == "text"        # "ok"
    assert body[1]["content"][1]["type"] == "tool_use"
    assert body[1]["content"][1]["name"] == "system.info"
    assert body[2]["role"] == "user"
    assert body[2]["content"][0]["type"] == "tool_result"
    assert body[2]["content"][0]["tool_use_id"] == "call_1"


async def test_anthropic_missing_key_yields_error():
    p = AnthropicProvider("")
    events = await _events(p, [ChatMessage("user", "hi")])
    assert events and events[0].kind == "error"


# ------------------------------------------------------------------ Gemini ---

def _gemini_body():
    return {
        "candidates": [{"content": {"parts": [
            {"text": "Hello"},
            {"functionCall": {"name": "system.info", "args": {"foo": "bar"}}},
        ], "role": "model"}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }


async def test_gemini_chat_text_and_function_call():
    def handler(request):
        return httpx.Response(200, json=_gemini_body())
    p = GeminiProvider("test-key", transport=httpx.MockTransport(handler))
    events = await _events(p, [ChatMessage("user", "hi")], tools=[
        {"type": "function", "function": {"name": "system.info"}}])
    text = "".join(e.text for e in events if e.kind == "token")
    assert text == "Hello"
    calls = [e.tool_calls for e in events if e.kind == "tool_calls"]
    assert calls and calls[0][0]["function"]["name"] == "system.info"
    assert json.loads(calls[0][0]["function"]["arguments"]) == {"foo": "bar"}


def test_gemini_message_conversion():
    p = GeminiProvider("k")
    msgs = [
        ChatMessage("system", "be terse"),
        ChatMessage("user", "run it"),
        ChatMessage("assistant", "ok", tool_calls=[
            {"id": "call_1", "function": {"name": "system.info", "arguments": "{}"}}]),
        ChatMessage("tool", "1 cpu", name="system.info", tool_call_id="call_1"),
    ]
    system, contents = p._messages(msgs)
    assert system == "be terse"
    assert contents[0] == {"role": "user", "parts": [{"text": "run it"}]}
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0] == {"text": "ok"}
    assert contents[1]["parts"][1]["functionCall"]["name"] == "system.info"
    assert contents[2]["role"] == "user"
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "system.info"


def test_gemini_schema_uppercases_types():
    from jarvis.llm.frontier import _gemini_schema
    out = _gemini_schema({"type": "object", "properties": {
        "a": {"type": "string"}, "b": {"type": "array", "items": {"type": "integer"}}}})
    assert out["type"] == "OBJECT"
    assert out["properties"]["a"]["type"] == "STRING"
    assert out["properties"]["b"]["items"]["type"] == "INTEGER"


# ------------------------------------------------------------------ factory ---

def test_build_frontier_kinds():
    assert isinstance(build_frontier("anthropic", "k", "m"), AnthropicProvider)
    assert isinstance(build_frontier("gemini", "k", "m"), GeminiProvider)
    assert isinstance(build_frontier("groq", "k", "m"), GroqProvider)
    with pytest.raises(UnavailableError):
        build_frontier("wat", "k", "m")


# ------------------------------------------------------------------- router ---

class _Secrets:
    def __init__(self, d):
        self._d = d

    def get(self, name, default=None):
        return self._d.get(name, default)


def _cfg(frontier_provider="", frontier_model="", frontier_key_secret=""):
    c = Config()
    c.llm.provider = "ollama"
    c.llm.model = "qwen2.5:3b-instruct"
    c.llm.fast_model = "qwen2.5:1.5b"
    c.llm.frontier_provider = frontier_provider
    c.llm.frontier_model = frontier_model
    c.llm.frontier_key_secret = frontier_key_secret
    return c


def test_router_frontier_route_with_key():
    c = _cfg(frontier_provider="groq", frontier_model="llama-3.3-70b",
             frontier_key_secret="groq_key")
    r = ModelRouter(c, secrets=_Secrets({"groq_key": "sk-123"}))
    route = r.route(want="frontier")
    assert route.provider.name == "groq"
    assert route.model == "llama-3.3-70b"


def test_router_frontier_unavailable_without_key():
    c = _cfg(frontier_provider="groq", frontier_model="llama-3.3-70b",
             frontier_key_secret="groq_key")
    r = ModelRouter(c, secrets=_Secrets({}))  # no key
    assert r.frontier is None
    with pytest.raises(UnavailableError):
        r.route(want="frontier")


def test_router_fast_and_primary_routes():
    c = _cfg()
    r = ModelRouter(c)
    assert r.route(want="fast").model == "qwen2.5:1.5b"
    assert r.route(want="primary").model == "qwen2.5:3b-instruct"


def test_router_privacy_local_failure_never_reaches_frontier():
    c = _cfg(frontier_provider="groq", frontier_model="llama-3.3-70b",
             frontier_key_secret="groq_key")
    r = ModelRouter(c, secrets=_Secrets({"groq_key": "sk-123"}))
    primary_route = r.route(want="primary")
    chain = r._fallback_chain(primary_route)
    # local primary/fallback chain must never contain the remote frontier
    assert all(p.name != "groq" for p in chain)


def test_router_frontier_failure_falls_back_to_local():
    c = _cfg(frontier_provider="groq", frontier_model="llama-3.3-70b",
             frontier_key_secret="groq_key")
    r = ModelRouter(c, secrets=_Secrets({"groq_key": "sk-123"}))
    frontier_route = r.route(want="frontier")
    chain = r._fallback_chain(frontier_route)
    # a failed remote call may fall back to local providers (safe direction)
    assert any(p.name == "ollama" for p in chain)


# ---------------------------------------------------- agent frontier routing ---

def _bare_agent():
    from jarvis.core.agent import Agent
    from jarvis.config import Config
    c = Config()
    c.llm.frontier_trigger = ["analyze", "research", "explain"]
    c.llm.fast_model = "qwen2.5:1.5b"
    a = object.__new__(Agent)
    a.config = c
    return a


class _NoFrontier:
    frontier = None


class _WithFrontier:
    class _F:
        pass
    frontier = _F()


def test_agent_frontier_trigger_matching():
    a = _bare_agent()
    assert a._frontier_triggered("please analyze this data")
    assert a._frontier_triggered("Explain how it works")
    assert not a._frontier_triggered("what time is it")
    assert not a._frontier_triggered("open steam")


def test_agent_route_tier_stays_local_without_frontier():
    a = _bare_agent()
    a.router = _NoFrontier()
    a.config.llm.frontier_provider = "groq"  # configured but no key -> router frontier None
    assert a._route_tier("analyze this", "text") == "primary"


def test_agent_route_tier_escalates_when_triggered():
    a = _bare_agent()
    a.router = _WithFrontier()
    a.config.llm.frontier_provider = "groq"
    a.config.llm.frontier_model = "llama-3.3-70b"
    assert a._route_tier("analyze this deeply", "text") == "frontier"
    # non-triggered request stays local even with frontier available
    assert a._route_tier("what time is it", "text") == "primary"
    # voice fast-path is preserved when not escalating
    assert a._route_tier("what time is it", "voice") == "fast"


def test_agent_route_tier_no_triggers_never_escalates():
    a = _bare_agent()
    a.config.llm.frontier_trigger = []  # empty -> privacy-first
    a.router = _WithFrontier()
    a.config.llm.frontier_provider = "groq"
    assert a._route_tier("analyze this deeply", "text") == "primary"
