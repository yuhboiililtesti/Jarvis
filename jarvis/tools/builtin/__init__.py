"""Builtin JARVIS tools.

Each module here defines one or more :class:`~jarvis.tools.base.BaseTool`
subclasses and registers them at import time via ``@register([...])``. The
loader in :func:`jarvis.tools.load_builtin_tools` imports every module in this
package, so importing a module is the only side effect a tool may rely on.

Modules whose runtime dependency is optional (psutil, Pillow) must import it
lazily so the Core still starts when the extra is absent; the tool then fails
with an honest, actionable message instead of crashing the loader.
"""

from __future__ import annotations

__all__: list[str] = []
