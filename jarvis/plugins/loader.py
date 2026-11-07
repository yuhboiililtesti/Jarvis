"""Owner-approved plugins: drop-in tool packs that extend JARVIS without editing the core.

Lifecycle (Blueprint §05): install → inspect → approve → pin → load → disable.

* A plugin is a folder under ``<data>/plugins/<id>/`` holding ``plugin.toml`` and
  the Python modules it lists. Modules register tools with the normal
  ``@register`` decorator, so a plugin tool goes through exactly the same
  policy, approval and verification path as a builtin one.
* Nothing loads until the owner enables it (``python -m jarvis plugins enable
  <id>``). Enabling pins a SHA-256 fingerprint of every file in the folder.
  If any file changes afterwards the plugin is refused until re-approved.
* A plugin may not replace an existing tool, and may not register a tool whose
  side effect exceeds the ``max_side_effect`` it declared — the owner approves
  that ceiling, so exceeding it backs the whole plugin out.

Honest limit: plugin code runs in-process with the Core's rights. The pin and
the ceiling stop silent change and over-reach by declared tools; they are not a
sandbox against hostile code. Only enable plugins you have read.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ids import now_iso
from ..logging_setup import get_logger
from ..tools.base import SideEffect
from ..tools.registry import REGISTRY, ToolRegistry

log = get_logger("jarvis.plugins")

__all__ = ["PluginManager", "PluginInfo", "fingerprint", "KV_KEY"]

KV_KEY = "plugins_enabled"          # {id: {"sha256": ..., "enabled_at": ...}}
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ORDER = [SideEffect.NONE, SideEffect.NETWORK, SideEffect.LOCAL_WRITE,
          SideEffect.LOCAL_EXEC, SideEffect.REMOTE, SideEffect.DESTRUCTIVE]


def fingerprint(folder: Path) -> str:
    """SHA-256 over every regular file (path + bytes), ignoring bytecode caches."""
    h = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        rel = path.relative_to(folder).as_posix()
        if "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
            continue
        h.update(rel.encode("utf-8") + b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


@dataclass
class PluginInfo:
    id: str
    folder: Path
    version: str = ""
    summary: str = ""
    modules: list[str] = field(default_factory=list)
    max_side_effect: str = SideEffect.NONE.value
    sha256: str = ""
    state: str = "discovered"       # discovered | disabled | changed | loaded | refused | invalid
    reason: str = ""
    tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "folder": str(self.folder), "version": self.version,
                "summary": self.summary, "modules": self.modules,
                "max_side_effect": self.max_side_effect, "sha256": self.sha256,
                "state": self.state, "reason": self.reason, "tools": self.tools}


class PluginManager:
    def __init__(self, root: Path, db: Any, registry: ToolRegistry = REGISTRY) -> None:
        self.root = Path(root)
        self.db = db
        self.registry = registry
        self.plugins: dict[str, PluginInfo] = {}

    # -- discovery -------------------------------------------------------
    def _approvals(self) -> dict[str, dict[str, Any]]:
        raw = self.db.kv_get(KV_KEY, {}) if self.db is not None else {}
        return raw if isinstance(raw, dict) else {}

    def discover(self) -> list[PluginInfo]:
        found: dict[str, PluginInfo] = {}
        if self.root.is_dir():
            for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
                if folder.name.startswith(("_", ".")):
                    continue
                info = self._read_manifest(folder)
                found[info.id] = info
        approvals = self._approvals()
        for info in found.values():
            if info.state == "invalid":
                continue
            pin = approvals.get(info.id)
            if pin is None:
                info.state, info.reason = "disabled", "not enabled by the owner"
            elif pin.get("sha256") != info.sha256:
                info.state = "changed"
                info.reason = "files changed since the owner approved it; re-enable to trust"
            else:
                info.state, info.reason = "discovered", "approved"
        self.plugins = found
        return list(found.values())

    def _read_manifest(self, folder: Path) -> PluginInfo:
        info = PluginInfo(id=folder.name, folder=folder)
        manifest = folder / "plugin.toml"
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except FileNotFoundError:
            info.state, info.reason = "invalid", "missing plugin.toml"
            return info
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            info.state, info.reason = "invalid", f"plugin.toml unreadable: {exc}"
            return info
        pid = str(data.get("id", "")).strip()
        modules = data.get("modules") or []
        ceiling = str(data.get("max_side_effect", SideEffect.NONE.value)).strip().lower()
        problems = []
        if not _ID_RE.match(pid):
            problems.append("id must be lowercase letters, digits, '-' or '_' (2-41 chars)")
        elif pid != folder.name:
            problems.append(f"id '{pid}' must match its folder name '{folder.name}'")
        if not isinstance(modules, list) or not modules:
            problems.append("modules must be a non-empty list")
        elif not all(isinstance(m, str) and _MODULE_RE.match(m) for m in modules):
            problems.append("module names must be plain Python identifiers")
        if ceiling not in {s.value for s in SideEffect}:
            problems.append(f"max_side_effect '{ceiling}' is not a side-effect class")
        info.id = pid or folder.name
        info.version = str(data.get("version", ""))
        info.summary = str(data.get("summary", ""))
        info.modules = [str(m) for m in modules] if isinstance(modules, list) else []
        info.max_side_effect = ceiling
        info.sha256 = fingerprint(folder)
        if problems:
            info.state, info.reason = "invalid", "; ".join(problems)
        return info

    # -- owner actions ---------------------------------------------------
    def enable(self, plugin_id: str) -> PluginInfo:
        """Approve the plugin's current files (pins their fingerprint)."""
        self.discover()
        info = self.plugins.get(plugin_id)
        if info is None:
            raise KeyError(f"no plugin '{plugin_id}' in {self.root}")
        if info.state == "invalid":
            raise ValueError(f"plugin '{plugin_id}' is invalid: {info.reason}")
        approvals = self._approvals()
        approvals[plugin_id] = {"sha256": info.sha256, "enabled_at": now_iso(),
                                "max_side_effect": info.max_side_effect}
        self.db.kv_set(KV_KEY, approvals)
        self.db.audit("plugin.enable", actor="owner", target=plugin_id,
                      detail={"sha256": info.sha256, "max_side_effect": info.max_side_effect},
                      severity="warn")
        info.state, info.reason = "discovered", "approved"
        return info

    def disable(self, plugin_id: str) -> bool:
        approvals = self._approvals()
        if approvals.pop(plugin_id, None) is None:
            return False
        self.db.kv_set(KV_KEY, approvals)
        self.db.audit("plugin.disable", actor="owner", target=plugin_id, severity="warn")
        return True

    # -- loading ---------------------------------------------------------
    def load_enabled(self) -> list[PluginInfo]:
        """Import every approved, unchanged plugin. Never raises."""
        for info in self.discover():
            if info.state != "discovered":
                continue
            try:
                self._load(info)
            except Exception as exc:  # a plugin must never take the Core down
                log.exception("plugin %s failed to load", info.id)
                self._back_out(info)
                info.state, info.reason = "refused", f"load error: {type(exc).__name__}: {exc}"
        return list(self.plugins.values())

    def _load(self, info: PluginInfo) -> None:
        before = set(self.registry.names())
        # Kept so a refused plugin that overwrote a tool can be undone exactly.
        self._originals = {n: (self.registry.get(n), self.registry.source_of(n))
                           for n in before}
        ceiling = _ORDER.index(SideEffect(info.max_side_effect))
        self.registry.loading_source = f"plugin:{info.id}"
        try:
            for mod in info.modules:
                path = info.folder / f"{mod}.py"
                if not path.is_file():
                    raise FileNotFoundError(f"module '{mod}.py' listed but missing")
                qualname = f"jarvis_plugin_{info.id.replace('-', '_')}.{mod}"
                spec = importlib.util.spec_from_file_location(qualname, path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot load {path}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[qualname] = module
                spec.loader.exec_module(module)
        finally:
            self.registry.loading_source = "builtin"

        added = [n for n in self.registry.names() if n not in before]
        replaced = [n for n in before
                    if self.registry.source_of(n) == f"plugin:{info.id}"]
        info.tools = added
        if replaced:
            # A plugin re-registered an existing name; the original is gone, so
            # the Core must not keep running with a silently swapped tool.
            raise PermissionError(f"plugin tried to replace existing tool(s): {replaced}")
        over = [n for n in added
                if _ORDER.index(self.registry.get(n).spec.side_effect) > ceiling]
        if over:
            raise PermissionError(
                f"tool(s) {over} exceed the approved ceiling '{info.max_side_effect}'")
        if not added:
            raise ValueError("plugin registered no tools")
        info.state, info.reason = "loaded", f"{len(added)} tool(s)"
        self.db.audit("plugin.load", target=info.id,
                      detail={"tools": added, "sha256": info.sha256})
        log.info("plugin %s loaded: %s", info.id, ", ".join(added))

    def _back_out(self, info: PluginInfo) -> None:
        originals = getattr(self, "_originals", {})
        for name in list(self.registry.names()):
            if self.registry.source_of(name) != f"plugin:{info.id}":
                continue
            self.registry.remove(name)
            if name in originals:
                tool, source = originals[name]
                self.registry.loading_source = source
                try:
                    self.registry.add(tool)
                finally:
                    self.registry.loading_source = "builtin"
        info.tools = []

    def status(self) -> dict[str, Any]:
        return {"root": str(self.root),
                "plugins": [p.to_dict() for p in self.plugins.values()]}
