"""Spoken-delivery persona mode and the serve bind guard."""

from __future__ import annotations

import json

from jarvis.__main__ import main
from jarvis.config import PersonaConfig
from jarvis.persona import Persona


def test_spoken_rules_only_for_voice():
    p = Persona(PersonaConfig())
    assert "SPOKEN DELIVERY" in p.system_prompt(spoken=True)
    assert "SPOKEN DELIVERY" not in p.system_prompt()


def test_agent_voice_turn_uses_spoken_prompt(kernel):
    seen: list[bool] = []
    original = kernel.persona.system_prompt

    def spy(**kw):
        seen.append(kw.get("spoken", False))
        return original(**kw)

    kernel.persona.system_prompt = spy

    async def no_model(*_a, **_kw):
        if False:
            yield None

    kernel.router.chat = no_model
    import asyncio

    async def run(source: str) -> None:
        async for _ in kernel.agent.run_turn("hello", session={"id": source}, source=source):
            pass

    asyncio.run(run("voice"))
    asyncio.run(run("text"))
    assert seen == [True, False]


def test_serve_refuses_non_loopback_without_lan(capsys):
    # Must refuse before importing uvicorn or building the kernel.
    assert main(["serve", "--host", "0.0.0.0"]) == 2
    assert "refusing to bind" in capsys.readouterr().out


def test_eval_suite_still_parses():
    from jarvis.eval import load_suite
    suite = load_suite("core")
    ids = {t.id for t in suite.tasks}
    assert {"weather_live", "fabrication_guard_calendar", "email_send_gated"} <= ids
    assert "fabrication_guard_weather" not in ids
    json.dumps([t.id for t in suite.tasks])
