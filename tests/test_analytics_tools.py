"""Analytics tools: statistics, forecasting, simulation and regression."""

from __future__ import annotations

import math

import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import analytics_tool as A
from jarvis.tools.builtin.analytics_tool import (
    linear_regression, mean, moving_average, stddev,
)


@pytest.fixture
def ctx(cfg):
    return ToolContext(config=cfg, db=None, bus=None)


# ---------------------------------------------------------------- helpers ---

def test_mean_and_stddev():
    assert mean([2, 4, 6]) == 4.0
    assert abs(stddev([2, 4, 6]) - math.sqrt(4.0)) < 1e-9  # sample stddev = 2


def test_linear_regression_perfect_line():
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [2.0, 4.0, 6.0, 8.0]  # y = 2x
    fit = linear_regression(xs, ys)
    assert abs(fit["slope"] - 2.0) < 1e-9
    assert abs(fit["intercept"]) < 1e-9
    assert abs(fit["r"] - 1.0) < 1e-9
    assert abs(fit["r2"] - 1.0) < 1e-9


def test_moving_average_window():
    assert moving_average([1, 2, 3, 4], 2) == [1.0, 1.5, 2.5, 3.5]


# ---------------------------------------------------------------- tools ---

async def test_stats_tool(ctx):
    r = await REGISTRY.call("compute.stats", ctx, data=[1, 2, 3, 4, 100])
    assert r.ok, r.error
    assert r.data["count"] == 5
    assert r.data["mean"] == 22.0
    assert r.data["median"] == 3.0
    assert r.data["min"] == 1 and r.data["max"] == 100
    assert r.data["quartiles"]["q2"] == 3.0


async def test_stats_rejects_bad_input(ctx):
    r = await REGISTRY.call("compute.stats", ctx, data=[1, "x", 3])
    assert not r.ok and "not a number" in r.error


async def test_forecast_linear(ctx):
    # a perfect linear series 2,4,6,8 -> next values should be 10,12,14
    r = await REGISTRY.call("compute.forecast", ctx, series=[2, 4, 6, 8],
                            horizon=3, method="linear")
    assert r.ok, r.error
    fc = r.data["forecast"]
    assert abs(fc[0] - 10.0) < 1e-6
    assert abs(fc[1] - 12.0) < 1e-6
    assert abs(fc[2] - 14.0) < 1e-6


async def test_forecast_rejects_bad_method(ctx):
    r = await REGISTRY.call("compute.forecast", ctx, series=[1, 2, 3], method="magic")
    assert not r.ok and "must be one of" in r.error


async def test_simulate_uniform_seeded(ctx):
    r = await REGISTRY.call("compute.simulate", ctx, trials=10000,
                            distribution="uniform", min=0, max=10, seed=42)
    assert r.ok, r.error
    # a uniform(0,10) sample must be near mean 5 and bounded [0,10]
    assert abs(r.data["mean"] - 5.0) < 0.3
    assert 0 <= r.data["min"] and r.data["max"] <= 10


async def test_simulate_is_reproducible(ctx):
    a = await REGISTRY.call("compute.simulate", ctx, trials=1000,
                            distribution="normal", mean=0, stddev=1, seed=7)
    b = await REGISTRY.call("compute.simulate", ctx, trials=1000,
                            distribution="normal", mean=0, stddev=1, seed=7)
    assert a.data["mean"] == b.data["mean"]


async def test_simulate_rejects_bad_distribution(ctx):
    r = await REGISTRY.call("compute.simulate", ctx, trials=10, distribution="gamma")
    assert not r.ok


async def test_regress_tool(ctx):
    r = await REGISTRY.call("compute.regress", ctx, x=[1, 2, 3], y=[2, 4, 6],
                            predict=[4])
    assert r.ok, r.error
    assert abs(r.data["slope"] - 2.0) < 1e-9
    assert abs(r.data["r"] - 1.0) < 1e-9
    assert abs(r.data["predictions"][0]["y"] - 8.0) < 1e-9


async def test_regress_rejects_mismatched_lengths(ctx):
    r = await REGISTRY.call("compute.regress", ctx, x=[1, 2], y=[1, 2, 3])
    assert not r.ok and "same length" in r.error
