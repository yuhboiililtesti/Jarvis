"""Time tools: current local time and timezone conversion.

Both are read-only. Conversion uses the stdlib `zoneinfo` database; when a zone
key is unknown (on Windows this usually means the optional `tzdata` wheel is not
installed) the tool fails with a clear, actionable message instead of guessing
an offset.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.time")

__all__ = ["TimeNowTool", "TimeConvertTool"]


def _zone(name: str):
    """Resolve an IANA zone name, treating 'UTC'/'Z' as the built-in UTC zone."""
    key = name.strip()
    if key.upper() in ("UTC", "Z", "GMT"):
        return timezone.utc
    return ZoneInfo(key)


def _describe(dt: datetime) -> dict[str, Any]:
    offset = dt.utcoffset()
    return {
        "iso": dt.isoformat(),
        "date": dt.date().isoformat(),
        "time": dt.strftime("%H:%M:%S"),
        "weekday": dt.strftime("%A"),
        "timezone": str(dt.tzinfo) if dt.tzinfo else None,
        "utc_offset_seconds": int(offset.total_seconds()) if offset else 0,
        "dst": bool(dt.dst()) if dt.dst() else False,
        "epoch": dt.timestamp() if dt.tzinfo else None,
    }


@register(["now", "time_now", "get_time"])
class TimeNowTool(BaseTool):
    spec = ToolSpec(
        name="time.now",
        summary="Current local time, date and timezone (optionally in a named zone).",
        category="time",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("tz", "string",
                      "Optional IANA timezone, e.g. 'Europe/London'. Defaults to local."),
        ),
        returns="object with iso, date, time, weekday, timezone and epoch",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            tz_name = (kwargs.get("tz") or "").strip()
            if tz_name:
                now = datetime.now(_zone(tz_name))
            else:
                now = datetime.now().astimezone()
            data = _describe(now)
            data["requested_tz"] = tz_name or None
            return ToolResult(ok=True, data=data)
        except ZoneInfoNotFoundError:
            return ToolResult(
                ok=False,
                error=(f"unknown timezone '{kwargs.get('tz')}' (on Windows install "
                       f"the 'tzdata' package for full IANA support)"),
            )
        except Exception as exc:
            log.warning("time.now failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["time_convert", "convert_time"])
class TimeConvertTool(BaseTool):
    spec = ToolSpec(
        name="time.convert",
        summary="Convert a timestamp between timezones (or 'now').",
        category="time",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("when", "string",
                      "ISO-8601 timestamp, or 'now'. Naive values use `from_tz`.",
                      required=True),
            ToolParam("to_tz", "string", "Target IANA timezone.", required=True),
            ToolParam("from_tz", "string",
                      "Source IANA timezone for a naive `when`. Defaults to local."),
        ),
        returns="object with the source and converted instants",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        try:
            raw = str(kwargs["when"]).strip()
            to_tz = str(kwargs["to_tz"]).strip()
            from_tz = str(kwargs.get("from_tz") or "").strip()

            if raw.lower() in ("", "now"):
                source = datetime.now().astimezone()
            else:
                text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
                try:
                    parsed = datetime.fromisoformat(text)
                except ValueError:
                    return ToolResult(
                        ok=False,
                        error=f"could not parse 'when' as ISO-8601: {raw!r}",
                    )
                if parsed.tzinfo is None:
                    tz = _zone(from_tz) if from_tz else datetime.now().astimezone().tzinfo
                    source = parsed.replace(tzinfo=tz)
                else:
                    source = parsed

            target = source.astimezone(_zone(to_tz))
            return ToolResult(ok=True, data={
                "source": _describe(source),
                "converted": _describe(target),
                "to_tz": to_tz,
            })
        except ZoneInfoNotFoundError as exc:
            return ToolResult(
                ok=False,
                error=(f"unknown timezone ({exc}); on Windows install the 'tzdata' "
                       f"package for full IANA support"),
            )
        except Exception as exc:
            log.warning("time.convert failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
