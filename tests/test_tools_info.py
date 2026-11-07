"""weather.* (fake HTTP transport, no network) and web.open (browser launch mocked)."""

from __future__ import annotations

import httpx
import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import SideEffect, ToolContext
from jarvis.tools.builtin import browser_tool, weather_tool

GEO = {"results": [
    {"name": "London", "latitude": 42.98, "longitude": -81.25, "country": "Canada",
     "country_code": "CA", "admin1": "Ontario", "timezone": "America/Toronto"},
    {"name": "London", "latitude": 51.5085, "longitude": -0.1257, "country": "United Kingdom",
     "country_code": "GB", "admin1": "England", "timezone": "Europe/London"},
]}
CURRENT = {"timezone": "Europe/London", "current": {
    "time": "2026-10-18T12:00", "temperature_2m": 14.2, "relative_humidity_2m": 80,
    "apparent_temperature": 13.0, "precipitation": 0.0, "weather_code": 3,
    "wind_speed_10m": 12.5, "wind_direction_10m": 230, "is_day": 1}}
DAILY = {"timezone": "Europe/London", "daily": {
    "time": ["2026-10-18", "2026-10-19"], "weather_code": [61, 0],
    "temperature_2m_max": [15.0, 16.5], "temperature_2m_min": [9.0, 8.5],
    "precipitation_sum": [3.2, 0.0], "precipitation_probability_max": [80, 5],
    "wind_speed_10m_max": [20.0, 11.0], "sunrise": ["2026-10-18T07:29", "2026-10-19T07:31"],
    "sunset": ["2026-10-18T18:00", "2026-10-19T17:58"]}}


@pytest.fixture
def ctx(cfg):
    return ToolContext(config=cfg, db=None, bus=None)


@pytest.fixture
def http():
    seen: list[httpx.Request] = []
    state = {"forecast_status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=GEO)
        if request.url.host == "api.open-meteo.com":
            if state["forecast_status"] != 200:
                return httpx.Response(state["forecast_status"])
            body = CURRENT if "current" in request.url.params else DAILY
            return httpx.Response(200, json=body)
        return httpx.Response(404)
    weather_tool.set_transport(httpx.MockTransport(handler))
    yield seen, state
    weather_tool.set_transport(None)


def test_weather_specs():
    for name in ("weather.current", "weather.forecast"):
        spec = REGISTRY.get(name).spec
        assert spec.side_effect == SideEffect.NETWORK and spec.needs_network


async def test_current_with_country_qualifier(ctx, http):
    seen, _ = http
    r = await REGISTRY.call("weather.current", ctx, location="London, GB")
    assert r.ok, r.error
    assert r.data["place"]["country_code"] == "GB"
    assert r.data["temperature"] == 14.2 and r.data["description"] == "overcast"
    fc = [q for q in seen if q.url.host == "api.open-meteo.com"][0]
    assert fc.url.params["latitude"] == "51.5085"
    assert "temperature_unit" not in fc.url.params


async def test_current_defaults_to_config_location(ctx, cfg, http):
    cfg.integrations.weather_location = "51.5,-0.12"
    seen, _ = http
    r = await REGISTRY.call("weather.current", ctx)
    assert r.ok and r.data["place"]["source"] == "config.integrations.weather_location"
    assert all(q.url.host != "geocoding-api.open-meteo.com" for q in seen)


async def test_imperial_units_forwarded(ctx, http):
    seen, _ = http
    r = await REGISTRY.call("weather.current", ctx, location="51.5,-0.12", units="imperial")
    assert r.ok and r.data["unit_labels"]["temp"] == "°F"
    assert seen[-1].url.params["temperature_unit"] == "fahrenheit"


async def test_forecast_days(ctx, http):
    r = await REGISTRY.call("weather.forecast", ctx, location="London, GB", days=2)
    assert r.ok and len(r.data["days"]) == 2
    assert r.data["days"][0] == {
        "date": "2026-10-18", "min": 9.0, "max": 15.0, "precipitation": 3.2,
        "precip_probability_pct": 80, "wind_max": 20.0, "weather_code": 61,
        "description": "slight rain", "sunrise": "2026-10-18T07:29", "sunset": "2026-10-18T18:00"}
    for bad in (0, 17, "x"):
        r = await REGISTRY.call("weather.forecast", ctx, location="London", days=bad)
        assert not r.ok


async def test_weather_errors(ctx, http):
    _, state = http
    r = await REGISTRY.call("weather.current", ctx)
    assert not r.ok and "weather_location" in r.error
    r = await REGISTRY.call("weather.current", ctx, location="London, ZZ")
    assert not r.ok and "no place found" in r.error
    r = await REGISTRY.call("weather.current", ctx, location="95,10")
    assert not r.ok and "out of range" in r.error
    state["forecast_status"] = 503
    r = await REGISTRY.call("weather.current", ctx, location="51,0")
    assert not r.ok and "503" in r.error


async def test_weather_network_failure_is_reported(ctx):
    def handler(request):
        raise httpx.ConnectError("offline")
    weather_tool.set_transport(httpx.MockTransport(handler))
    try:
        r = await REGISTRY.call("weather.current", ctx, location="51,0")
        assert not r.ok and "ConnectError" in r.error
    finally:
        weather_tool.set_transport(None)


# ---------------------------------------------------------------- web.open ---

@pytest.fixture
def opened(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_tool, "_open_url", lambda url: calls.append(url) or True)
    monkeypatch.setattr(browser_tool, "health", lambda: {"available": True, "reason": "test"})
    return calls


def test_web_open_spec():
    spec = REGISTRY.get("web.open").spec
    assert spec.side_effect == SideEffect.LOCAL_EXEC
    assert "dry_run" in [p.name for p in spec.params]


async def test_web_open_ok_and_unverified(ctx, opened):
    tool = REGISTRY.get("web.open")
    r = await tool.run(ctx, url="https://open-meteo.com/en/docs")
    assert r.ok and opened == ["https://open-meteo.com/en/docs"]
    assert tool.verify(ctx, r) is None


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/System32/calc.exe", "javascript:alert(1)", "ms-settings:privacy",
    "ftp://example.com/x", "https://user:pw@example.com/", "https://", "http://exa mple.com",
    "https://example.com/\nx", "notaurl", "https://example.com/" + "a" * 3000,
])
async def test_web_open_refuses_bad_urls(ctx, opened, url):
    r = await REGISTRY.get("web.open").run(ctx, url=url)
    assert not r.ok
    assert opened == []


async def test_web_open_dry_run(ctx, opened):
    r = await REGISTRY.get("web.open").run(ctx, url="https://example.com", dry_run=True)
    assert r.ok and r.data["dry_run"] and opened == []
