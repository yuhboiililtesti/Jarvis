"""Smart home via the Home Assistant REST API (self-hosted, free).

Tools:
  * ``home.states`` (NETWORK) - read entity states.
  * ``home.call_service`` (REMOTE) - *low-impact* services only: lights, media
    players, fans, ordinary switches/covers, input helpers, vacuums, remotes, TTS.
  * ``home.call_service_critical`` (DESTRUCTIVE, always needs approval) - any
    service, including the high-impact ones ``home.call_service`` refuses:
    locks, alarm panels, valves, water heaters, sirens, climate (setpoints/HVAC),
    garage/gate/door covers, oven/heater/boiler switches, scripts, automations,
    scenes, buttons and every domain not on the low-impact allow-list.
    Classification uses each target entity's *current* attributes (device_class,
    friendly name) fetched before the call; a call with no ``entity_id`` on a
    switch/cover (i.e. every device in the domain) is high-impact.

Configuration: ``config.integrations.home_assistant_url`` plus the secret
``home_assistant`` (a long-lived access token). Without both the tools return
"not configured". The token is sent only in the Authorization header and is
never included in results or errors.

Read-back: after a service call each targeted entity is re-read. ``verified``
is true only when every entity reached the expected state for that service
(turn_on -> on, lock -> locked, ...), or, for services without a known end
state, when its ``last_updated`` moved past the pre-call snapshot.
Entity attributes are returned as data, never interpreted.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common

log = get_logger("jarvis.tools.home")

__all__ = ["HomeStatesTool", "HomeCallServiceTool", "HomeCallServiceCriticalTool",
           "classify_call", "set_transport", "health", "LOW_IMPACT_DOMAINS"]

LOW_IMPACT_DOMAINS = frozenset({"light", "media_player", "fan", "switch", "cover", "remote",
                                "vacuum", "input_boolean", "input_select", "input_number",
                                "tts"})
_HIGH_COVER_CLASSES = frozenset({"garage", "gate", "door"})
_HEAT_WORDS = ("oven", "heater", "heating", "stove", "boiler", "kettle", "hob", "furnace",
               "radiator", "iron")
_NAME = re.compile(r"^[a-z0-9_]+$")
_ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_EXPECTED = {
    # media players and climate devices report their mode rather than "on"
    "turn_on": {"on", "idle", "playing", "paused", "standby", "heat", "cool", "heat_cool",
                "auto", "dry", "fan_only"},
    "turn_off": {"off"}, "lock": {"locked", "locking"},
    "unlock": {"unlocked", "unlocking"}, "open": {"open", "opening"},
    "open_cover": {"open", "opening"}, "close_cover": {"closed", "closing"},
    "open_valve": {"open", "opening"}, "close_valve": {"closed", "closing"},
    "media_play": {"playing"}, "media_pause": {"paused"}, "media_stop": {"idle", "off", "paused"},
    "alarm_disarm": {"disarmed"}, "alarm_arm_home": {"armed_home", "arming"},
    "alarm_arm_away": {"armed_away", "arming"}, "alarm_arm_night": {"armed_night", "arming"},
    "start": {"cleaning"}, "return_to_base": {"returning", "docked"},
}
_MAX_DATA_BYTES = 8192

_TRANSPORT: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _TRANSPORT
    _TRANSPORT = transport


def _settings(ctx: ToolContext) -> tuple[str, str]:
    integ = getattr(getattr(ctx, "config", None), "integrations", None)
    url = str(getattr(integ, "home_assistant_url", "") or "").strip().rstrip("/")
    lookup = getattr(ctx, "secrets", None)
    token = ""
    if callable(lookup):
        try:
            token = str(lookup("home_assistant") or "").strip()
        except Exception:
            token = ""
    return url, token


def health(ctx: ToolContext | None = None) -> dict[str, Any]:
    if ctx is None:
        return _common.health_ok("httpx available; configuration checked per call")
    url, token = _settings(ctx)
    if not url:
        return _common.health_fail("Home Assistant is not configured: set "
                                   "integrations.home_assistant_url")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return _common.health_fail("integrations.home_assistant_url must be an http(s) URL")
    if not token:
        return _common.health_fail("Home Assistant is not configured: add the secret "
                                   "'home_assistant' (long-lived access token)")
    return _common.health_ok("configured", url=url)


def _client(url: str, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=url, timeout=15.0, transport=_TRANSPORT,
                             follow_redirects=False,
                             headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json"})


def _summ(st: dict[str, Any]) -> dict[str, Any]:
    attrs = st.get("attributes") or {}
    return {"entity_id": st.get("entity_id"), "state": st.get("state"),
            "friendly_name": attrs.get("friendly_name"),
            "device_class": attrs.get("device_class"),
            "unit": attrs.get("unit_of_measurement"),
            "last_changed": st.get("last_changed"), "last_updated": st.get("last_updated")}


async def _get_state(client: httpx.AsyncClient, entity_id: str) -> dict[str, Any] | None:
    resp = await client.get(f"/api/states/{entity_id}")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"Home Assistant returned HTTP {resp.status_code} for {entity_id}")
    return resp.json()


def classify_call(domain: str, service: str, entities: list[str],
                  states: dict[str, dict[str, Any] | None]) -> tuple[str, str]:
    """Return ("low"|"high", reason)."""
    if domain not in LOW_IMPACT_DOMAINS:
        return "high", f"domain '{domain}' is not on the low-impact list"
    if not entities and domain in ("switch", "cover"):
        return "high", f"{domain}.{service} without entity_id targets every {domain}"
    for eid in entities:
        edomain = eid.split(".", 1)[0]
        if edomain not in LOW_IMPACT_DOMAINS:
            return "high", f"target {eid} is in high-impact domain '{edomain}'"
        st = states.get(eid) or {}
        attrs = st.get("attributes") or {}
        if edomain == "cover" and str(attrs.get("device_class") or "") in _HIGH_COVER_CLASSES:
            return "high", f"{eid} is a {attrs.get('device_class')} cover"
        if edomain == "switch":
            label = f"{eid} {attrs.get('friendly_name') or ''}".lower()
            if any(w in label for w in _HEAT_WORDS):
                return "high", f"{eid} looks like a heating appliance"
    return "low", "low-impact domain and targets"


def _entities(kwargs: dict[str, Any]) -> list[str]:
    raw = kwargs.get("entity_id")
    if raw in (None, "", []):
        return []
    items = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else None
    if items is None:
        raise ValueError("entity_id must be a string or list of strings")
    out = []
    for e in items:
        e = str(e).strip().lower()
        if not _ENTITY.match(e):
            raise ValueError(f"invalid entity_id {e!r}")
        out.append(e)
    if len(out) > 50:
        raise ValueError("at most 50 entity_ids per call")
    return out


@register(["ha.states", "home.status"])
class HomeStatesTool(BaseTool):
    spec = ToolSpec(
        name="home.states",
        summary="Read Home Assistant entity states (all, one entity, or one domain).",
        category="home",
        side_effect=SideEffect.NETWORK,
        needs_network=True,
        params=(
            ToolParam("entity_id", "string", "One entity, e.g. light.kitchen."),
            ToolParam("domain", "string", "Filter by domain, e.g. light."),
            ToolParam("limit", "integer", "Maximum entities returned (1-500).", default=200),
        ),
        returns="object: {count, entities:[{entity_id,state,friendly_name,device_class,...}]}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _common.unavailable_result(health(ctx))
        if gate:
            return gate
        url, token = _settings(ctx)
        try:
            entity = str(kwargs.get("entity_id") or "").strip().lower()
            domain = str(kwargs.get("domain") or "").strip().lower()
            if entity and not _ENTITY.match(entity):
                return ToolResult(ok=False, error=f"invalid entity_id {entity!r}")
            if domain and not _NAME.match(domain):
                return ToolResult(ok=False, error=f"invalid domain {domain!r}")
            try:
                limit = max(1, min(int(kwargs.get("limit") or 200), 500))
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="limit must be an integer")
            async with _client(url, token) as client:
                if entity:
                    st = await _get_state(client, entity)
                    if st is None:
                        return ToolResult(ok=False, error=f"unknown entity {entity}")
                    items = [st]
                else:
                    resp = await client.get("/api/states")
                    if resp.status_code == 401:
                        return ToolResult(ok=False, error="Home Assistant rejected the token (401)")
                    if resp.status_code != 200:
                        return ToolResult(ok=False,
                                          error=f"Home Assistant returned HTTP {resp.status_code}")
                    items = resp.json() or []
            if domain:
                items = [s for s in items if str(s.get("entity_id", "")).startswith(domain + ".")]
            out = [_summ(s) for s in items[:limit]]
            return ToolResult(ok=True, data={"count": len(out), "total": len(items),
                                             "entities": out})
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"Home Assistant request failed: {type(exc).__name__}")
        except Exception as exc:
            log.warning("home.states failed: %s", type(exc).__name__)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


class _CallService(BaseTool):
    critical = False

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _common.unavailable_result(health(ctx))
        if gate:
            return gate
        url, token = _settings(ctx)
        try:
            domain = str(kwargs.get("domain") or "").strip().lower()
            service = str(kwargs.get("service") or "").strip().lower()
            if not _NAME.match(domain) or not _NAME.match(service):
                return ToolResult(ok=False, error="domain and service must be lowercase "
                                                  "identifiers, e.g. light / turn_on")
            try:
                entities = _entities(kwargs)
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            data = kwargs.get("data") or {}
            if not isinstance(data, dict):
                return ToolResult(ok=False, error="data must be an object")
            if "entity_id" in data:
                return ToolResult(ok=False, error="pass entity_id as its own parameter, not in data")
            import json as _json
            if len(_json.dumps(data, default=str)) > _MAX_DATA_BYTES:
                return ToolResult(ok=False, error="data is too large")

            async with _client(url, token) as client:
                before: dict[str, dict[str, Any] | None] = {}
                for eid in entities:
                    before[eid] = await _get_state(client, eid)
                missing = [e for e, s in before.items() if s is None]
                if missing:
                    return ToolResult(ok=False, error=f"unknown entity: {', '.join(missing)}")
                impact, reason = classify_call(domain, service, entities, before)
                if impact == "high" and not self.critical:
                    return ToolResult(ok=False, evidence={"refused": True, "impact": "high"},
                                      error=(f"refused: high-impact call ({reason}); use "
                                             "home.call_service_critical, which always needs "
                                             "owner approval"))
                expected = sorted(_EXPECTED.get(service, set()))
                if _common.dry_run_requested(kwargs):
                    return _common.preview(
                        target=[_summ(s) for s in before.values() if s] or f"all {domain} entities",
                        operation=f"{domain}.{service}",
                        effect=(f"call {domain}.{service}"
                                + (f"; expected state: {'/'.join(expected)}" if expected else "")),
                        reversible="depends on the device",
                        rollback=("call the opposite service (e.g. turn_off after turn_on); "
                                  "previous states are listed in target"),
                        impact=impact, impact_reason=reason, data=data)
                payload = dict(data)
                if entities:
                    payload["entity_id"] = entities if len(entities) > 1 else entities[0]
                resp = await client.post(f"/api/services/{domain}/{service}", json=payload)
            if resp.status_code == 401:
                return ToolResult(ok=False, error="Home Assistant rejected the token (401)")
            if resp.status_code >= 400:
                return ToolResult(ok=False, error=(f"Home Assistant returned HTTP "
                                                   f"{resp.status_code} for {domain}.{service}"))
            return ToolResult(
                ok=True,
                data={"domain": domain, "service": service, "entities": entities,
                      "impact": impact, "expected_states": expected,
                      "before": {e: _summ(s) for e, s in before.items() if s}},
                evidence={"method": "ha-rest", "status": resp.status_code},
                certainty="unverified",
            )
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=f"Home Assistant request failed: {type(exc).__name__}",
                              certainty="unknown")
        except Exception as exc:
            log.warning("%s failed: %s", self.spec.name, type(exc).__name__)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        entities = result.data.get("entities") or []
        if not entities:
            return None   # domain-wide call: no specific entity to read back
        url, token = _settings(ctx)
        expected = set(result.data.get("expected_states") or [])
        before = result.data.get("before") or {}
        observed: dict[str, Any] = {}
        all_ok = True
        try:
            async with _client(url, token) as client:
                for _ in range(6):          # read-back polling, not a retry of the effect
                    all_ok = True
                    for eid in entities:
                        st = await _get_state(client, eid)
                        observed[eid] = _summ(st) if st else None
                        if st is None:
                            all_ok = False
                        elif expected:
                            all_ok &= st.get("state") in expected
                        else:
                            all_ok &= st.get("last_updated") != (before.get(eid) or {}).get(
                                "last_updated")
                    if all_ok:
                        break
                    await asyncio.sleep(0.5)
        except Exception as exc:
            return {"verified": False, "method": "ha-state-readback",
                    "note": f"read-back failed: {type(exc).__name__}"}
        return {"verified": all_ok, "method": "ha-state-readback",
                "expected": sorted(expected) or "last_updated changed", "observed": observed}


def _svc_params() -> tuple[ToolParam, ...]:
    return (
        ToolParam("domain", "string", "Service domain, e.g. light.", required=True),
        ToolParam("service", "string", "Service name, e.g. turn_on.", required=True),
        ToolParam("entity_id", "string", "Target entity id (or a list of them)."),
        ToolParam("data", "object", "Extra service data, e.g. {\"brightness_pct\": 40}."),
        _common.DRY_RUN_PARAM,
    )


@register(["ha.call", "home.service"])
class HomeCallServiceTool(_CallService):
    critical = False
    spec = ToolSpec(
        name="home.call_service",
        summary=("Call a low-impact Home Assistant service (lights, media, fans, ordinary "
                 "switches/covers). High-impact devices are refused."),
        category="home",
        side_effect=SideEffect.REMOTE,
        needs_network=True,
        params=_svc_params(),
        returns="object: {domain, service, entities, impact, expected_states, before}",
    )


@register(["ha.call_critical"])
class HomeCallServiceCriticalTool(_CallService):
    critical = True
    spec = ToolSpec(
        name="home.call_service_critical",
        summary=("Call any Home Assistant service, including locks, alarms, garage/gate "
                 "covers, climate, valves and heating appliances (always needs approval)."),
        category="home",
        side_effect=SideEffect.DESTRUCTIVE,
        needs_network=True,
        params=_svc_params(),
        returns="object: {domain, service, entities, impact, expected_states, before}",
        cheap=False,
    )
