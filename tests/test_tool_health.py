"""Standardized health probes (P2): resolution order and honest statuses."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.tools.health import (CATEGORY_PROBES, probe_category, probe_tool,
                                 summarize)


def _spec(category: str) -> SimpleNamespace:
    return SimpleNamespace(category=category, name="test.tool")


def test_module_health_wins_when_present():
    module = SimpleNamespace(health=lambda: {"available": True, "reason": "ok"})
    result = probe_tool(_spec("documents"), module)
    assert result["status"] == "live"
    assert result["method"] == "module-health"


def test_module_health_reports_unavailable_with_reason():
    module = SimpleNamespace(
        health=lambda: {"available": False, "reason": "no psutil"})
    result = probe_tool(_spec("documents"), module)
    assert result["status"] == "unavailable"
    assert "psutil" in result["reason"]


def test_category_probe_for_documents_depends_on_real_packages(monkeypatch):
    # force a missing package and confirm the probe truthfully reports it
    import jarvis.tools.health as H
    real = H._has_package
    monkeypatch.setattr(H, "_has_package",
                        lambda name: False if name == "docx" else real(name))
    result = probe_category("documents")
    assert result is not None
    assert result["available"] is False
    assert "docx" in result["reason"]
    assert result["method"] == "category-packages"


def test_unknown_category_is_unverified_not_unhealthy():
    result = probe_tool(_spec("no-such-category"), None)
    assert result["status"] == "unverified"
    assert result["available"] is None
    assert result["method"] == "none"


def test_platform_probe_matches_the_host():
    result = probe_category("input")
    assert result is not None
    import os
    assert result["available"] == (os.name == "nt")


def test_summarize_counts_every_status():
    entries = [{"status": "live"}, {"status": "unverified"},
               {"status": "unavailable"}, {"status": "live"}]
    assert summarize(entries) == {"live": 2, "unavailable": 1,
                                  "unverified": 1}


def test_every_configured_probe_is_resolvable():
    for category in CATEGORY_PROBES:
        assert probe_category(category) is not None, category


def test_inventory_statuses_are_consistent(tmp_path):
    import json
    from jarvis.tools import REGISTRY, load_builtin_tools
    from jarvis.tools.health import probe_tool
    import sys

    load_builtin_tools()
    statuses = set()
    for name in REGISTRY.names():
        tool = REGISTRY.get(name)
        module = sys.modules.get(type(tool).__module__)
        statuses.add(probe_tool(tool.spec, module)["status"])
    assert statuses <= {"live", "unavailable", "unverified"}
    assert "live" in statuses
