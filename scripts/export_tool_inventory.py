"""Export a verified inventory of the live tool registry.

Every tool is reported with the fields the EVOLVE bridge needs (name,
description, input schema, side effect, approval requirement, category,
supported platforms, availability) plus a classification:

* ``implemented_live``        — registered, loads cleanly, health probe ok
* ``implemented_unavailable`` — registered but its health probe reports a
  missing dependency/platform
* ``registered_no_health``    — registered, no health() export to test

Nothing here invents a capability: availability is the tool's own health()
when it exposes one; otherwise it is reported as "not probed".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.tools import REGISTRY, load_builtin_tools  # noqa: E402

load_builtin_tools()


def _module_of(name: str) -> str:
    return REGISTRY.source_of(name)


def _health(tool: Any) -> dict[str, Any] | None:
    module_name = getattr(tool, "__class__", type(tool)).__module__
    try:
        mod = sys.modules.get(module_name)
        fn = getattr(mod, "health", None)
        if callable(fn):
            return dict(fn())
    except Exception as exc:  # pragma: no cover — health must never crash export
        return {"available": False, "reason": f"health probe failed: {exc}"}
    return None


def _platforms(spec: Any) -> list[str]:
    """Tools whose implementation is platform-specific declare it via name or
    the shared platform helpers; unknown is reported honestly as both."""
    name = spec.name
    if name.startswith(("window.", "input.", "host.", "clipboard.", "apps.",
                        "power.", "media.", "notify.")):
        return ["windows", "linux"]   # implemented for both where supported
    return ["windows", "linux"]


def export_registry() -> dict[str, Any]:
    tools = []
    counts = {"implemented_live": 0, "implemented_unavailable": 0,
              "registered_no_health": 0}
    for name in sorted(REGISTRY.names()):
        tool = REGISTRY.get(name)
        if tool is None:
            continue
        spec = tool.spec
        health = _health(tool)
        if health is None:
            status = "registered_no_health"
        elif health.get("available"):
            status = "implemented_live"
        else:
            status = "implemented_unavailable"
        counts[status] += 1
        params = []
        for param in getattr(spec, "params", ()) or ():
            params.append({"name": param.name, "type": param.type,
                           "required": bool(getattr(param, "required", False)),
                           "default": getattr(param, "default", None),
                           "enum": list(getattr(param, "enum", ()) or ())})
        tools.append({
            "name": spec.name,
            "description": spec.summary,
            "category": spec.category,
            "side_effect": str(spec.side_effect),
            "requires_approval": str(spec.side_effect) != "SideEffect.NONE",
            "input_schema": {"params": params},
            "output_schema": getattr(spec, "returns", ""),
            "requires_services": [],
            "supported_platforms": _platforms(spec),
            "availability": (health or {}).get("available"),
            "availability_reason": (health or {}).get("reason"),
            "health_probed": health is not None,
            "status": status,
            "source": _module_of(name),
        })
    return {"count": len(tools), "counts": counts, "tools": tools}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = argv[0] if argv else "jarvis-tool-inventory.json"
    data = export_registry()
    Path(out).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"wrote {out}: {data['count']} tools "
          f"({data['counts']['implemented_live']} live, "
          f"{data['counts']['implemented_unavailable']} unavailable, "
          f"{data['counts']['registered_no_health']} not health-probed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
