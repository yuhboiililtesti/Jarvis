"""JARVIS shim over the standalone ``evolve`` package.

The evolutionary engine lives in its own project (``C:\\Users\\Topaz\\Projects\\Evolve``,
installed editable). This module re-exports the whole core so existing JARVIS
code and tools keep working unchanged, and adds the JARVIS-specific service.
"""

from evolve import *  # noqa: F401,F403 — re-export the core platform
from evolve import __all__ as _evolve_all
from .service import EvolveService

__all__ = list(_evolve_all) + ["EvolveService"]
