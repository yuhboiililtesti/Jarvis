"""JARVIS tools subsystem."""

from .base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from .registry import REGISTRY, ToolRegistry, register

__all__ = [
    "BaseTool", "SideEffect", "ToolContext", "ToolParam", "ToolResult",
    "ToolSpec", "REGISTRY", "ToolRegistry", "register", "load_builtin_tools",
]


def load_builtin_tools() -> int:
    """Import every builtin tool module so their @register hooks fire."""
    import importlib
    import pkgutil
    from pathlib import Path

    pkg_dir = Path(__file__).parent / "builtin"
    count = 0
    if not pkg_dir.is_dir():
        return 0
    for mod in pkgutil.iter_modules([str(pkg_dir)]):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"jarvis.tools.builtin.{mod.name}")
        count += 1
    return count
