"""``weather.current`` / ``weather.forecast`` via Open-Meteo (free, no API key).

Location resolution: ``location`` may be ``"lat,lon"`` or a place name
("Leeds" or "Leeds, GB"); names are resolved with the Open-Meteo geocoding API.
When omitted, ``config.integrations.weather_location`` is used; with neither,
the tool says so instead of guessing.

Output is strictly structured data (numbers, units, ISO times, WMO codes and a
fixed English description table). Free text from the remote side is limited to
the geocoder's place/country/admin names, returned as data fields only.

Tests inject an ``httpx.MockTransport`` through :func:`set_transport`, so no
network is needed.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common, _win

log = get_logger("jarvis.tools.weather")

__all__ = ["WeatherCurrentTool", "WeatherForecastTool", "set_transport", "health",
           "WMO_CODES", "GEOCODE_URL", "FORECAST_URL"]

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_UA = "Jarvis/1.0 (+local-first personal assistant)"
_LATLON = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
    48: "depositing rime fog", 51: "light drizzle", 53: "moderate drizzle",
    55: "dense drizzle", 56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain", 66: "light freezing rain",
    67: "heavy freezing rain", 71: "slight snow", 73: "moderate snow", 75: "heavy snow",
    77: "snow grains", 80: "slight rain showers", 81: "moderate rain showers",
    82: "violent rain showers", 85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}

_TRANSPORT: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _TRANSPORT
    _TRANSPORT = transport


def health() -> dict[str, Any]:
    return _common.health_ok("httpx + Open-Meteo (needs internet access)")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15.0, transport=_TRANSPORT, headers={"User-Agent": _UA},
                             follow_redirects=False)


def _units(kwargs: dict[str, Any]) -> tuple[str, dict[str, str], dict[str, str]]:
    units = str(kwargs.get("units") or "metric").lower()
    if units not in ("metric", "imperial"):
        raise ValueError("units must be metric or imperial")
    if units == "imperial":
        return units, {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                       "precipitation_unit": "inch"}, {"temp": "°F", "wind": "mph",
                                                       "precip": "in"}
    return units, {}, {"temp": "°C", "wind": "km/h", "precip": "mm"}


async def _resolve(ctx: ToolContext, kwargs: dict[str, Any], client: httpx.AsyncClient
                   ) -> dict[str, Any]:
    raw = str(kwargs.get("location") or "").strip()
    source = "argument"
    if not raw:
        integ = getattr(getattr(ctx, "config", None), "integrations", None)
        raw = str(getattr(integ, "weather_location", "") or "").strip()
        source = "config.integrations.weather_location"
    if not raw:
        raise ValueError("no location given and config.integrations.weather_location is empty")
    if len(raw) > 120:
        raise ValueError("location is too long")
    m = _LATLON.match(raw)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("latitude/longitude out of range")
        return {"name": None, "latitude": lat, "longitude": lon, "country": None,
                "admin1": None, "source": source}
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    name, qualifiers = parts[0], [q.lower() for q in parts[1:]]
    resp = await client.get(GEOCODE_URL, params={"name": name, "count": 10, "language": "en",
                                                 "format": "json"})
    if resp.status_code != 200:
        raise RuntimeError(f"geocoding returned HTTP {resp.status_code}")
    results = (resp.json() or {}).get("results") or []

    def _match(r: dict[str, Any]) -> bool:
        fields = {str(r.get(k) or "").lower() for k in ("country_code", "country", "admin1")}
        return all(q in fields for q in qualifiers)
    chosen = next((r for r in results if _match(r)), None)
    if chosen is None:
        raise LookupError(f"no place found for {raw!r}")
    return {"name": chosen.get("name"), "latitude": chosen.get("latitude"),
            "longitude": chosen.get("longitude"), "country": chosen.get("country"),
            "country_code": chosen.get("country_code"), "admin1": chosen.get("admin1"),
            "timezone": chosen.get("timezone"), "source": source}


def _num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


_LOCATION_P = ToolParam("location", "string",
                        "Place name ('Leeds, GB') or 'lat,lon'; defaults to config weather_location.")
_UNITS_P = ToolParam("units", "string", "metric or imperial.", default="metric",
                     enum=("metric", "imperial"))


async def _forecast(ctx: ToolContext, kwargs: dict[str, Any], params: dict[str, Any]
                    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], str]:
    units, unit_params, labels = _units(kwargs)
    async with _client() as client:
        place = await _resolve(ctx, kwargs, client)
        q = {"latitude": place["latitude"], "longitude": place["longitude"],
             "timezone": "auto", **unit_params, **params}
        resp = await client.get(FORECAST_URL, params=q)
        if resp.status_code != 200:
            raise RuntimeError(f"forecast returned HTTP {resp.status_code}")
        return place, resp.json() or {}, labels, units


def _fail(exc: Exception, name: str) -> ToolResult:
    if isinstance(exc, (ValueError, LookupError)):
        return ToolResult(ok=False, error=str(exc))
    if isinstance(exc, httpx.TimeoutException):
        return ToolResult(ok=False, error="weather service timed out")
    if isinstance(exc, httpx.HTTPError):
        return ToolResult(ok=False, error=f"weather request failed: {type(exc).__name__}")
    log.warning("%s failed: %s", name, exc)
    return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["weather", "weather.now"])
class WeatherCurrentTool(BaseTool):
    spec = ToolSpec(
        name="weather.current",
        summary="Current weather for a place (Open-Meteo, no API key).",
        category="info",
        side_effect=SideEffect.NETWORK,
        needs_network=True,
        params=(_LOCATION_P, _UNITS_P),
        returns="object: {place, observed_at, temperature, feels_like, humidity_pct, "
                "precipitation, wind_speed, wind_direction_deg, weather_code, description}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            place, body, labels, units = await _forecast(ctx, kwargs, {
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                           "precipitation,weather_code,wind_speed_10m,wind_direction_10m,is_day"})
            cur = body.get("current") or {}
            if not cur:
                return ToolResult(ok=False, error="weather service returned no current data")
            code = _num(cur.get("weather_code"))
            return ToolResult(ok=True, data={
                "place": place, "units": units, "unit_labels": labels,
                "observed_at": cur.get("time"), "timezone": body.get("timezone"),
                "temperature": _num(cur.get("temperature_2m")),
                "feels_like": _num(cur.get("apparent_temperature")),
                "humidity_pct": _num(cur.get("relative_humidity_2m")),
                "precipitation": _num(cur.get("precipitation")),
                "wind_speed": _num(cur.get("wind_speed_10m")),
                "wind_direction_deg": _num(cur.get("wind_direction_10m")),
                "is_day": bool(cur.get("is_day")) if cur.get("is_day") is not None else None,
                "weather_code": code, "description": WMO_CODES.get(code, "unknown"),
                "source": "open-meteo.com",
            })
        except Exception as exc:
            return _fail(exc, "weather.current")


@register(["forecast", "weather.daily"])
class WeatherForecastTool(BaseTool):
    spec = ToolSpec(
        name="weather.forecast",
        summary="Daily weather forecast for up to 16 days (Open-Meteo, no API key).",
        category="info",
        side_effect=SideEffect.NETWORK,
        needs_network=True,
        params=(_LOCATION_P, _UNITS_P,
                ToolParam("days", "integer", "Number of days (1-16).", default=3)),
        returns="object: {place, days:[{date, min, max, precipitation, precip_probability_pct, "
                "wind_max, weather_code, description, sunrise, sunset}]}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            try:
                days = _win.int_arg(kwargs, "days", 3)
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            if days is None or not 1 <= days <= 16:
                return ToolResult(ok=False, error="days must be between 1 and 16")
            place, body, labels, units = await _forecast(ctx, kwargs, {
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                         "precipitation_probability_max,wind_speed_10m_max,sunrise,sunset",
                "forecast_days": days})
            d = body.get("daily") or {}
            dates = d.get("time") or []
            if not dates:
                return ToolResult(ok=False, error="weather service returned no daily data")

            def col(key: str, i: int) -> Any:
                vals = d.get(key) or []
                return vals[i] if i < len(vals) else None
            out = []
            for i, date in enumerate(dates[:days]):
                code = _num(col("weather_code", i))
                out.append({
                    "date": date, "min": _num(col("temperature_2m_min", i)),
                    "max": _num(col("temperature_2m_max", i)),
                    "precipitation": _num(col("precipitation_sum", i)),
                    "precip_probability_pct": _num(col("precipitation_probability_max", i)),
                    "wind_max": _num(col("wind_speed_10m_max", i)),
                    "weather_code": code, "description": WMO_CODES.get(code, "unknown"),
                    "sunrise": col("sunrise", i), "sunset": col("sunset", i),
                })
            return ToolResult(ok=True, data={"place": place, "units": units,
                                             "unit_labels": labels,
                                             "timezone": body.get("timezone"), "days": out,
                                             "source": "open-meteo.com"})
        except Exception as exc:
            return _fail(exc, "weather.forecast")
