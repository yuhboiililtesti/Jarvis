"""Natural-language calculate tool and its evaluator."""

from __future__ import annotations

import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import calculate_tool
from jarvis.tools.builtin.calculate_tool import natural_math


@pytest.mark.parametrize("q,expected", [
    ("what is 15% of 80", "12"),
    ("15% of 200", "30"),
    ("25 is what percent of 200", "12.5"),
    ("what is 2+3*4", "14"),
    ("square root of 144", "12"),
    ("3 plus 4", "7"),
    ("what is 7 plus 8", "15"),
    ("3 squared plus 4 squared", "25"),
    ("5 times 6", "30"),
    ("100 divided by 4", "25"),
])
def test_natural_math_answers(q, expected):
    r = natural_math(q)
    assert r is not None, q
    assert expected in r["answer"]


@pytest.mark.parametrize("q,expected", [
    ("convert 5 miles to km", "8.04672"),
    ("convert 100 celsius to fahrenheit", "212"),
    ("convert 32 fahrenheit to celsius", "0"),
])
def test_natural_math_converts(q, expected):
    r = natural_math(q)
    assert r is not None, q
    assert expected in r["answer"]


def test_natural_math_returns_none_when_not_math():
    assert natural_math("hello there") is None


async def test_calculate_tool_builtin(cfg):
    ctx = ToolContext(config=cfg, db=None, bus=None, services={})
    r = await REGISTRY.call("calculate.solve", ctx, question="what is 15% of 80")
    assert r.ok and "12" in r.data["answer"]
    assert r.data["engine"] == "builtin"


async def test_calculate_tool_unknown_question(cfg):
    ctx = ToolContext(config=cfg, db=None, bus=None, services={})
    r = await REGISTRY.call("calculate.solve", ctx, question="what color is the sky")
    assert not r.ok and "could not understand" in r.error


async def test_calculate_tool_prefers_compute_service(cfg):
    class FakeCompute:
        async def ask(self, question):
            return {"status": "solved", "method": "relation", "answer": "42 m/s"}

    ctx = ToolContext(config=cfg, db=None, bus=None, services={"compute": FakeCompute()})
    r = await REGISTRY.call("calculate.solve", ctx, question="how fast")
    assert r.ok and r.data["answer"] == "42 m/s"
    assert r.data["engine"] == "algokit"
