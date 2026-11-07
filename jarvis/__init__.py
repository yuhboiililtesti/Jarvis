"""JARVIS — a local-first personal assistant.

Core runs on APOS (brain, tools, memory, policy, orchestration).
PC-001 runs as a client/agent (HUD, voice, screen/vision, local tools).
Node runs on a satellite machine (mic/speaker/sensors relay).
Nothing here requires a paid API or imposes a token cap.
"""

from .version import VERSION, banner

__version__ = VERSION
__all__ = ["VERSION", "banner", "__version__"]
