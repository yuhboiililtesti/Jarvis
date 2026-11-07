"""Model router (local-first)."""

from __future__ import annotations

from .service import ModelRouterService


class Router(ModelRouterService):
    pass


__all__ = ["ModelRouterService", "Router"]
