"""JARVIS node (satellite) subsystem.

The Core side lives in :mod:`jarvis.nodes.service` (``NodeService``, built by the
kernel). The satellite side lives in :mod:`jarvis.nodes.agent` and is started on
the ThinkPad with ``python -m jarvis.nodes``. Both share the wire contract in
:mod:`jarvis.nodes.protocol`.
"""

from __future__ import annotations

from .service import NodeService

__all__ = ["NodeService"]
