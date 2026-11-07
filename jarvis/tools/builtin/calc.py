"""Calculator tool: safe arithmetic evaluation.

Arithmetic is parsed to an AST and evaluated against a strict whitelist of node
types, operators, functions and constants. `eval()` and `compile()` are never
used, attribute access is impossible, and results are bounded so a hostile
expression cannot exhaust the host.
"""

from __future__ import annotations

import ast
import math
from typing import Any, Callable

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.calc")

__all__ = ["CalcEvalTool"]

_MAX_INPUT = 512          # characters
_MAX_NODES = 200          # AST nodes
_MAX_POW_EXP = 1000       # |exponent| bound
_MAX_FACTORIAL = 1000     # n! bound


class CalcError(ValueError):
    """A user-facing arithmetic/syntax error (never a crash)."""


def _safe_factorial(n: Any) -> int:
    if not isinstance(n, int) or isinstance(n, bool):
        raise CalcError("factorial() requires an integer")
    if n < 0 or n > _MAX_FACTORIAL:
        raise CalcError(f"factorial() argument must be between 0 and {_MAX_FACTORIAL}")
    return math.factorial(n)


_FUNCS: dict[str, Callable[..., Any]] = {
    "abs": abs, "round": round, "min": min, "max": max, "pow": math.pow,
    "sqrt": math.sqrt, "exp": math.exp, "log": math.log, "log2": math.log2,
    "log10": math.log10, "floor": math.floor, "ceil": math.ceil,
    "trunc": math.trunc, "fabs": math.fabs, "factorial": _safe_factorial,
    "gcd": math.gcd, "hypot": math.hypot, "degrees": math.degrees,
    "radians": math.radians, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "copysign": math.copysign, "fmod": math.fmod,
}

_CONSTS: dict[str, float] = {
    "pi": math.pi, "e": math.e, "tau": math.tau,
    "inf": math.inf, "nan": math.nan,
}


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CalcError(f"unsupported literal: {value!r}")
        return value

    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise CalcError(f"unknown name '{node.id}'")

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, (ast.Div, ast.FloorDiv, ast.Mod)):
            if right == 0:
                raise CalcError("division by zero")
            if isinstance(op, ast.Div):
                return left / right
            if isinstance(op, ast.FloorDiv):
                return left // right
            return left % right
        if isinstance(op, ast.Pow):
            if abs(right) > _MAX_POW_EXP:
                raise CalcError("exponent too large")
            try:
                return left ** right
            except OverflowError:
                raise CalcError("result too large")
        raise CalcError("unsupported binary operator")

    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.Not):
            return not value
        raise CalcError("unsupported unary operator")

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise CalcError("only direct function calls are allowed")
        fn = _FUNCS.get(node.func.id)
        if fn is None:
            raise CalcError(f"unknown function '{node.func.id}'")
        if node.keywords:
            raise CalcError("keyword arguments are not allowed")
        args = [_eval_node(a) for a in node.args]
        if node.func.id == "pow" and len(args) == 2 and abs(args[1]) > _MAX_POW_EXP:
            raise CalcError("exponent too large")
        try:
            return fn(*args)
        except CalcError:
            raise
        except (ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
            raise CalcError(f"{node.func.id}() error: {exc}")

    raise CalcError(f"unsupported syntax: {type(node).__name__}")


@register(["calc", "evaluate", "math"])
class CalcEvalTool(BaseTool):
    spec = ToolSpec(
        name="calc.eval",
        summary=("Evaluate a safe arithmetic expression (+ - * / // % **, "
                 "parentheses and common math functions)."),
        category="math",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("expression", "string", "Arithmetic expression to evaluate.",
                      required=True),
        ),
        returns="object with the numeric result and its type",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        expr = str(kwargs["expression"])
        try:
            if len(expr) > _MAX_INPUT:
                return ToolResult(ok=False,
                                  error=f"expression too long (max {_MAX_INPUT} chars)")
            try:
                tree = ast.parse(expr, mode="eval")
            except (SyntaxError, ValueError, MemoryError) as exc:
                return ToolResult(ok=False, error=f"invalid expression: {exc}")
            if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
                return ToolResult(ok=False, error="expression too complex")
            value = _eval_node(tree)
            return ToolResult(ok=True, data={
                "expression": expr,
                "result": value,
                "type": type(value).__name__,
                "repr": repr(value),
            })
        except CalcError as exc:
            return ToolResult(ok=False, error=str(exc) or "invalid expression")
        except Exception as exc:  # never raise out of run()
            log.warning("calc.eval failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
