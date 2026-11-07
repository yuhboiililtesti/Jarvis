"""OpenAI-compatible serving endpoints (/v1/models, /v1/chat/completions, /v1/embeddings)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jarvis.api.openai_compat import build_openai_router
from jarvis.llm.providers import StreamEvent


class _FakeProvider:
    default_model = "qwen2.5:3b-instruct"
    embed_model = "nomic-embed-text"

    async def list_models(self):
        return ["qwen2.5:3b-instruct", "nomic-embed-text"]

    async def chat(self, messages, *, model=None, tools=None, temperature=None,
                   num_ctx=None, num_predict=None):
        yield StreamEvent("token", text="hello")
        yield StreamEvent("meta", meta={"eval_count": 2, "prompt_eval_count": 3})
        yield StreamEvent("done")

    async def embed(self, text, *, model=None):
        return [0.1, 0.2, 0.3]


class _FakeRouter:
    def __init__(self):
        self.primary = _FakeProvider()

    async def embed(self, text):
        return await self.primary.embed(text)


class _FakeLLM:
    model = "qwen2.5:3b-instruct"
    embed_model = "nomic-embed-text"


class _FakeConfig:
    llm = _FakeLLM()


class _FakeKernel:
    def __init__(self):
        self.router = _FakeRouter()
        self.config = _FakeConfig()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(build_openai_router(_FakeKernel()))
    return TestClient(app)


def test_models(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert "qwen2.5:3b-instruct" in ids


def test_chat_completions_non_streaming(client):
    r = client.post("/v1/chat/completions", json={
        "model": "qwen2.5:3b-instruct",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["completion_tokens"] == 2


def test_chat_completions_streaming(client):
    with client.stream("POST", "/v1/chat/completions", json={
        "model": "qwen2.5:3b-instruct",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }) as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())
    assert "chat.completion.chunk" in text
    assert "[DONE]" in text


def test_chat_completions_requires_messages(client):
    r = client.post("/v1/chat/completions", json={"model": "x"})
    assert r.status_code == 400


def test_embeddings(client):
    r = client.post("/v1/embeddings", json={"input": "hello", "model": "nomic-embed-text"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]
