"""Natural-language calculator: ``calculate.solve``.

Answers plain-language questions like "what is 15% of 80", "convert 5 miles to
km", "square root of 144" or "3 squared plus 4 squared" *without* requiring the
caller to write an expression. It reuses the safe AST evaluator in
:mod:`jarvis.tools.builtin.calc` (``eval``/``compile`` are never used) and adds a
thin natural-language layer on top. When the optional ``compute`` service
(algokit) is available it is asked first, so richer physics/finance answers are
used where the toolkit is installed.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from .calc import _eval_node  # safe AST evaluator (shared with calc.eval)

log = get_logger("jarvis.tools.calculate")

__all__ = ["CalculateSolveTool", "natural_math"]

_MAX_INPUT = 512
_MAX_NODES = 200

_FILLER = re.compile(
    r"^(?:what\s+(?:is|are)|what's|whats|how\s+much\s+is|calculate|compute|"
    r"evaluate|solve|work\s+out|tell\s+me|please|the\s+value\s+of)\s+",
    re.IGNORECASE,
)
_TRAILING = re.compile(r"\s*[?=.]+\s*$")

# Linear unit conversions: (from_unit, to_unit) -> multiplier.
_LINEAR_UNITS: dict[tuple[str, str], float] = {
    ("mile", "km"): 1.609344, ("miles", "km"): 1.609344,
    ("km", "mile"): 0.621371, ("km", "miles"): 0.621371,
    ("kg", "lb"): 2.20462, ("kg", "lbs"): 2.20462,
    ("lb", "kg"): 0.453592, ("lbs", "kg"): 0.453592,
    ("meter", "foot"): 3.28084, ("meters", "feet"): 3.28084,
    ("foot", "meter"): 0.3048, ("feet", "meters"): 0.3048,
    ("inch", "cm"): 2.54, ("inches", "cm"): 2.54,
    ("cm", "inch"): 0.393701, ("cm", "inches"): 0.393701,
    ("gallon", "liter"): 3.78541, ("gallons", "liters"): 3.78541,
    ("liter", "gallon"): 0.264172, ("liters", "gallons"): 0.264172,
}


def _safe_eval(expr: str) -> Any:
    if len(expr) > _MAX_INPUT:
        raise ValueError("expression too long")
    tree = ast.parse(expr, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
        raise ValueError("expression too complex")
    return _eval_node(tree)


def _to_expression(text: str) -> str | None:
    """Reduce wordy arithmetic to a bare expression, or ``None``."""
    t = text.strip()
    t = re.sub(r"\b(?:the\s+)?square\s+root\s+of\s+([\w.()*/^+-]+)", r"sqrt(\1)", t,
               flags=re.IGNORECASE)
    t = re.sub(r"\b(?:the\s+)?cube\s+root\s+of\s+([\w.()*/^+-]+)", r"(\1)**(1/3)", t,
               flags=re.IGNORECASE)
    t = re.sub(r"\b([\w.()]+)\s+squared\b", r"(\1)**2", t, flags=re.IGNORECASE)
    t = re.sub(r"\b([\w.()]+)\s+cubed\b", r"(\1)**3", t, flags=re.IGNORECASE)
    for word, op in (("divided by", "/"), ("multiplied by", "*"),
                     ("plus", "+"), ("minus", "-"), ("times", "*")):
        t = re.sub(rf"\b{word}\b", op, t, flags=re.IGNORECASE)
    t = t.replace("^", "**")  # '^' means power to a person, not Python XOR
    t = _FILLER.sub("", t, count=1)
    t = _TRAILING.sub("", t)
    t = t.strip()
    if not t or t.lower() == text.strip().lower():
        return None
    return t


_SINGULAR = {
    "miles": "mile", "meters": "meter", "feet": "foot", "inches": "inch",
    "gallons": "gallon", "liters": "liter", "litres": "liter", "lbs": "lb",
}


def _convert(text: str) -> dict[str, Any] | None:
    """Handle ``convert X <unit> to <unit>`` / ``how many <unit> in X <unit>``."""
    m = re.search(r"convert\s+(-?\d+(?:\.\d+)?)\s*([A-Za-z]+)\s+(?:to|into|in)\s+"
                  r"([A-Za-z]+)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"how\s+many\s+([A-Za-z]+)\s+(?:are\s+)?in\s+"
                      r"(-?\d+(?:\.\d+)?)\s*([A-Za-z]+)", text, re.IGNORECASE)
        if m:
            to_unit, value, from_unit = m.group(1), float(m.group(2)), m.group(3)
        else:
            return None
    else:
        value, from_unit, to_unit = float(m.group(1)), m.group(2), m.group(3)

    f, t = from_unit.lower(), to_unit.lower()
    # temperature uses an offset, so handle it before any plural normalisation
    if f in ("c", "celsius", "centigrade") and t in ("f", "fahrenheit"):
        return {"answer": f"{_fmt(value)} °C = {_fmt(value * 9 / 5 + 32)} °F",
                "method": "unit-conversion"}
    if f in ("f", "fahrenheit") and t in ("c", "celsius", "centigrade"):
        return {"answer": f"{_fmt(value)} °F = {_fmt((value - 32) * 5 / 9)} °C",
                "method": "unit-conversion"}

    f = _SINGULAR.get(f, f)
    t = _SINGULAR.get(t, t)
    if (f, t) in _LINEAR_UNITS:
        return {"answer": f"{value} {from_unit} = "
                          f"{_fmt(value * _LINEAR_UNITS[(f, t)])} {to_unit}",
                "method": "unit-conversion"}
    return None


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value == int(value) and abs(value) < 1e16:
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def natural_math(question: str) -> dict[str, Any] | None:
    """Best-effort natural-language arithmetic; ``None`` when not understood."""
    text = (question or "").strip()
    if not text:
        return None

    # percentages --------------------------------------------------------
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%\s*of\s*(-?\d+(?:\.\d+)?)", text)
    if m:
        pct, val = float(m.group(1)), float(m.group(2))
        return {"answer": f"{_fmt(pct)}% of {_fmt(val)} = {_fmt(pct / 100 * val)}",
                "method": "percent"}
    m = re.search(r"(-?\d+(?:\.\d+)?)\s+is\s+what\s+percent(?:age)?\s+of\s+"
                  r"(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m:
        part, whole = float(m.group(1)), float(m.group(2))
        if whole == 0:
            return None
        return {"answer": f"{_fmt(part)} is {_fmt(part / whole * 100)}% of {_fmt(whole)}",
                "method": "percent"}

    # unit conversion ----------------------------------------------------
    conv = _convert(text)
    if conv is not None:
        return conv

    # plain arithmetic ---------------------------------------------------
    expr = _to_expression(text)
    if expr is not None:
        try:
            return {"answer": _fmt(_safe_eval(expr)), "method": "arithmetic"}
        except Exception as exc:
            log.debug("calculate eval failed for %r: %s", text, exc)
            return None
    return None


@register(["calculate", "calculate.solve", "calc.natural", "math.ask"])
class CalculateSolveTool(BaseTool):
    spec = ToolSpec(
        name="calculate.solve",
        summary=("Answer a natural-language math question, e.g. 'what is 15% of 80', "
                 "'convert 5 miles to km', 'square root of 144', '3 plus 4'."),
        category="math",
        side_effect=SideEffect.NONE,
        params=(ToolParam("question", "string", "Plain-language math question.",
                          required=True),),
        returns="object with the answer and the method used",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        question = str(kwargs.get("question") or "").strip()
        if not question:
            return ToolResult(ok=False, error="question is required")

        # Prefer the compute service (algokit) when it is present and can answer
        # in natural language; fall back to the built-in evaluator.
        svc = ctx.service("compute") if hasattr(ctx, "service") else None
        if svc is not None and hasattr(svc, "ask"):
            try:
                res = await svc.ask(question)
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("compute.ask failed: %s", exc)
                res = None
            if isinstance(res, dict) and res.get("status") == "solved":
                return ToolResult(ok=True, data={"question": question,
                                                 "answer": res.get("answer"),
                                                 "method": res.get("method"),
                                                 "engine": "algokit"})

        result = natural_math(question)
        if result is None:
            return ToolResult(ok=False, data={"question": question},
                              error="could not understand that as a calculation")
        return ToolResult(ok=True, data={"question": question,
                                         "answer": result.get("answer"),
                                         "method": result.get("method"),
                                         "engine": "builtin"})
