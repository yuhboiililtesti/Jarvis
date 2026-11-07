"""Analytics tools: statistics, forecasting, simulation and regression.

Self-contained and dependency-free (pure Python; no numpy required), so these
work on the Core (APOS) with zero extra installs. Each tool computes real numbers
and returns them with the method and inputs it used — never a placeholder, never
a fabricated result.
"""

from __future__ import annotations

import math
import random
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.analytics")

__all__ = [
    "StatsTool", "ForecastTool", "SimulateTool", "RegressionTool",
    "mean", "stddev", "linear_regression", "moving_average", "exponential_smoothing",
]


# ---------------------------------------------------------------------------
# pure helpers (also exported for reuse and testing)
# ---------------------------------------------------------------------------

def _numbers(values: Any) -> list[float]:
    """Coerce a list/tuple/space-or-comma string of numbers to floats."""
    if isinstance(values, (list, tuple)):
        raw = list(values)
    elif isinstance(values, str):
        raw = [x for x in values.replace(",", " ").split() if x.strip()]
    else:
        raise ValueError("expected a list of numbers")
    out: list[float] = []
    for v in raw:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            raise ValueError(f"not a number: {v!r}")
    return out


def mean(xs: list[float]) -> float:
    if not xs:
        raise ValueError("no values")
    return sum(xs) / len(xs)


def _median(xs: list[float]) -> float:
    n = len(xs)
    if n == 0:
        raise ValueError("no values")
    s = sorted(xs)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _mode(xs: list[float]) -> list[float]:
    counts: dict[float, int] = {}
    for x in xs:
        counts[x] = counts.get(x, 0) + 1
    top = max(counts.values(), default=0)
    return sorted(k for k, v in counts.items() if v == top and top > 1)


def _variance(xs: list[float], ddof: int = 1) -> float:
    n = len(xs)
    if n <= ddof:
        raise ValueError("need more values for variance")
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - ddof)


def stddev(xs: list[float]) -> float:
    return math.sqrt(_variance(xs))


def _percentile(xs: list[float], p: float) -> float:
    """Linear-interpolation percentile (0..100)."""
    if not xs:
        raise ValueError("no values")
    s = sorted(xs)
    n = len(s)
    if n == 1:
        return s[0]
    rank = (p / 100.0) * (n - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return s[lo]
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _skewness(xs: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    m = mean(xs)
    sd = stddev(xs)
    if sd == 0:
        return 0.0
    return (n / ((n - 1) * (n - 2))) * sum(((x - m) / sd) ** 3 for x in xs)


def _kurtosis(xs: list[float]) -> float:
    n = len(xs)
    if n < 4:
        return 0.0
    m = mean(xs)
    sd = stddev(xs)
    if sd == 0:
        return 0.0
    return (n * (n + 1) / ((n - 1) * (n - 2) * (n - 3))) * sum(
        ((x - m) / sd) ** 4 for x in xs) - (3 * (n - 1) ** 2) / ((n - 2) * (n - 3))


def linear_regression(xs: list[float], ys: list[float]) -> dict[str, float]:
    """Ordinary least squares. Returns slope, intercept, r, r2, mae, rmse."""
    n = len(xs)
    if n != len(ys):
        raise ValueError("x and y must be the same length")
    if n < 2:
        raise ValueError("need at least two points")
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("x values are constant; cannot fit a line")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    # correlation
    syy = sum((y - my) ** 2 for y in ys)
    r = sxy / math.sqrt(sxx * syy) if syy else 0.0
    preds = [slope * x + intercept for x in xs]
    mae = sum(abs(p - y) for p, y in zip(preds, ys)) / n
    rmse = math.sqrt(sum((p - y) ** 2 for p, y in zip(preds, ys)) / n)
    return {"slope": slope, "intercept": intercept, "r": r, "r2": r * r,
            "mae": mae, "rmse": rmse}


def moving_average(xs: list[float], window: int) -> list[float]:
    if window < 1:
        raise ValueError("window must be >= 1")
    out: list[float] = []
    for i in range(len(xs)):
        lo = max(0, i - window + 1)
        out.append(mean(xs[lo:i + 1]))
    return out


def exponential_smoothing(xs: list[float], alpha: float) -> list[float]:
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    if not xs:
        return []
    out = [xs[0]]
    for x in xs[1:]:
        out.append(alpha * x + (1 - alpha) * out[-1])
    return out


def _forecast_linear(xs: list[float], horizon: int) -> list[float]:
    t = list(range(len(xs)))
    fit = linear_regression(t, xs)
    return [fit["slope"] * (len(xs) + i) + fit["intercept"] for i in range(horizon)]


def _forecast_smoothing(xs: list[float], horizon: int, alpha: float) -> list[float]:
    smoothed = exponential_smoothing(xs, alpha)
    last = smoothed[-1]
    return [last for _ in range(horizon)]  # flat forecast from the smoothed level


def _rng(distribution: str, params: dict[str, float], rng: random.Random) -> float:
    if distribution == "uniform":
        lo = float(params.get("min", 0.0))
        hi = float(params.get("max", 1.0))
        return rng.uniform(lo, hi)
    if distribution == "normal":
        m = float(params.get("mean", 0.0))
        sd = float(params.get("stddev", 1.0))
        return rng.gauss(m, sd)
    raise ValueError(f"unknown distribution {distribution!r} (uniform|normal)")


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

@register(["stats", "compute.stats"])
class StatsTool(BaseTool):
    spec = ToolSpec(
        name="compute.stats",
        summary="Descriptive statistics for a list of numbers (mean, median, spread, percentiles).",
        category="compute",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("data", "array", "List of numbers.", required=True),
            ToolParam("percentiles", "array", "Optional percentiles (0-100), e.g. [25,50,90]."),
        ),
        returns="object with count, sum, mean, median, mode, min, max, range, variance, stddev, quartiles, skewness, kurtosis, percentiles",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            xs = _numbers(kwargs.get("data"))
            if not xs:
                return ToolResult(ok=False, error="data must be a non-empty list of numbers")
            pcts = kwargs.get("percentiles")
            pct_list = _numbers(pcts) if pcts else []
            if any(not 0 <= p <= 100 for p in pct_list):
                return ToolResult(ok=False, error="percentiles must be between 0 and 100")
            data: dict[str, Any] = {
                "count": len(xs),
                "sum": sum(xs),
                "mean": mean(xs),
                "median": _median(xs),
                "mode": _mode(xs),
                "min": min(xs),
                "max": max(xs),
                "range": max(xs) - min(xs),
                "variance": _variance(xs),
                "stddev": stddev(xs),
                "quartiles": {"q1": _percentile(xs, 25), "q2": _percentile(xs, 50),
                              "q3": _percentile(xs, 75)},
                "skewness": _skewness(xs),
                "kurtosis": _kurtosis(xs),
                "percentiles": {str(p): _percentile(xs, p) for p in pct_list},
            }
            return ToolResult(ok=True, data=data)
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))


@register(["forecast", "compute.forecast"])
class ForecastTool(BaseTool):
    spec = ToolSpec(
        name="compute.forecast",
        summary=("Forecast the next values of a numeric series "
                 "(linear trend, moving average, or exponential smoothing)."),
        category="compute",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("series", "array", "Time-ordered list of numbers.", required=True),
            ToolParam("horizon", "integer", "How many future points to forecast.", default=3),
            ToolParam("method", "string", "Forecast method.", default="linear",
                      enum=("linear", "moving_average", "exp_smoothing")),
            ToolParam("window", "integer", "Window for moving_average.", default=3),
            ToolParam("alpha", "number", "Smoothing factor (0-1] for exp_smoothing.", default=0.3),
        ),
        returns="object with forecast, method, parameters, and last-value error",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            xs = _numbers(kwargs.get("series"))
            if len(xs) < 2:
                return ToolResult(ok=False, error="series must have at least 2 points")
            horizon = int(kwargs.get("horizon", 3))
            if horizon < 1 or horizon > 100:
                return ToolResult(ok=False, error="horizon must be 1-100")
            method = str(kwargs.get("method", "linear")).lower()
            if method == "linear":
                fc = _forecast_linear(xs, horizon)
                params: dict[str, Any] = {"method": "linear"}
            elif method == "moving_average":
                window = int(kwargs.get("window", 3))
                ma = moving_average(xs, window)
                fc = [ma[-1] for _ in range(horizon)]
                params = {"method": "moving_average", "window": window}
            elif method == "exp_smoothing":
                alpha = float(kwargs.get("alpha", 0.3))
                fc = _forecast_smoothing(xs, horizon, alpha)
                params = {"method": "exp_smoothing", "alpha": alpha}
            else:
                return ToolResult(ok=False, error=f"unknown method {method!r}")
            # one-step-back error: how well the model tracked the last point
            error = abs(xs[-1] - (fc[0] if fc else xs[-1]))
            return ToolResult(ok=True, data={
                "forecast": fc, "last_actual": xs[-1], "last_error": error,
                "params": params, "horizon": horizon,
            })
        except (ValueError, TypeError) as exc:
            return ToolResult(ok=False, error=str(exc))


@register(["simulate", "compute.simulate"])
class SimulateTool(BaseTool):
    spec = ToolSpec(
        name="compute.simulate",
        summary="Monte Carlo simulation of a uniform or normal distribution.",
        category="compute",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("trials", "integer", "Number of samples.", required=True),
            ToolParam("distribution", "string", "uniform | normal.", default="uniform",
                      enum=("uniform", "normal")),
            ToolParam("min", "number", "Uniform lower bound.", default=0),
            ToolParam("max", "number", "Uniform upper bound.", default=1),
            ToolParam("mean", "number", "Normal mean.", default=0),
            ToolParam("stddev", "number", "Normal standard deviation.", default=1),
            ToolParam("seed", "integer", "Optional RNG seed for reproducibility."),
        ),
        returns="object with summary statistics of the simulated outcomes",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            trials = int(kwargs.get("trials"))
            if trials < 1 or trials > 1_000_000:
                return ToolResult(ok=False, error="trials must be 1-1,000,000")
            dist = str(kwargs.get("distribution", "uniform")).lower()
            seed = kwargs.get("seed")
            rng = random.Random(int(seed)) if seed is not None else random.Random()
            params = {k: float(v) for k, v in
                      (("min", kwargs.get("min", 0)), ("max", kwargs.get("max", 1)),
                       ("mean", kwargs.get("mean", 0)), ("stddev", kwargs.get("stddev", 1)))}
            samples = [_rng(dist, params, rng) for _ in range(trials)]
            data = {
                "trials": trials, "distribution": dist, "params": params,
                "mean": mean(samples), "median": _median(samples),
                "stddev": stddev(samples) if trials > 1 else 0.0,
                "min": min(samples), "max": max(samples),
                "p5": _percentile(samples, 5), "p95": _percentile(samples, 95),
            }
            if seed is not None:
                data["seed"] = int(seed)
            return ToolResult(ok=True, data=data)
        except (ValueError, TypeError) as exc:
            return ToolResult(ok=False, error=str(exc))


@register(["regress", "compute.regress"])
class RegressionTool(BaseTool):
    spec = ToolSpec(
        name="compute.regress",
        summary="Linear regression and correlation between two equal-length series.",
        category="compute",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("x", "array", "Independent variable values.", required=True),
            ToolParam("y", "array", "Dependent variable values.", required=True),
            ToolParam("predict", "array", "Optional x values to predict y for."),
        ),
        returns="object with slope, intercept, correlation (r), r2, mae, rmse, and predictions",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            xs = _numbers(kwargs.get("x"))
            ys = _numbers(kwargs.get("y"))
            fit = linear_regression(xs, ys)
            data: dict[str, Any] = dict(fit)
            data["n"] = len(xs)
            predict = kwargs.get("predict")
            if predict:
                pxs = _numbers(predict)
                data["predictions"] = [{"x": x, "y": fit["slope"] * x + fit["intercept"]}
                                       for x in pxs]
            return ToolResult(ok=True, data=data)
        except (ValueError, TypeError) as exc:
            return ToolResult(ok=False, error=str(exc))
