"""system.info / system.metrics: structure, honest GPU reporting and CSV parsing.

These tests are hardware-independent: they assert the *shape* of the response and
the honest-failure contract, never a specific CPU/GPU value, so they pass on a
machine with or without an NVIDIA GPU.
"""

from __future__ import annotations

import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import system_info


@pytest.fixture
def ctx(cfg):
    return ToolContext(config=cfg, db=None, bus=None)


# ---------------------------------------------------------------- parsing ---

def test_parse_gpu_csv_realistic_row():
    line = ("0, NVIDIA GeForce RTX 3080, 10240, 645, 9595, 9, 61, 82.0, 320.0, "
            "42, 1440, 9501")
    gpus = system_info._parse_gpu_csv([line])
    assert len(gpus) == 1
    g = gpus[0]
    assert g["index"] == 0
    assert g["name"] == "NVIDIA GeForce RTX 3080"
    assert g["memory_total_mib"] == 10240
    assert g["memory_used_mib"] == 645
    assert g["memory_free_mib"] == 9595
    assert g["utilization_percent"] == 9
    assert g["temperature_c"] == 61
    assert g["power_watts"] == 82.0
    assert g["power_limit_watts"] == 320.0
    assert g["fan_percent"] == 42
    assert g["clock_graphics_mhz"] == 1440
    assert g["clock_memory_mhz"] == 9501


def test_parse_gpu_csv_na_fields_are_none_not_zero():
    line = "0, NVIDIA GTX 1660 SUPER, 6144, N/A, N/A, 0, 35, N/A, N/A, N/A, N/A, N/A"
    g = system_info._parse_gpu_csv([line])[0]
    assert g["memory_total_mib"] == 6144
    assert g["memory_used_mib"] is None
    assert g["temperature_c"] == 35
    assert g["power_watts"] is None
    assert g["fan_percent"] is None


def test_parse_gpu_process_csv():
    line = "1234, ollama_llama_server, 2160"
    procs = system_info._parse_gpu_process_csv([line])
    assert procs == [{"pid": 1234, "name": "ollama_llama_server",
                      "memory_used_mib": 2160}]


# ---------------------------------------------------------------- honesty ---

def test_gpu_snapshot_reports_unavailable_when_no_reader(monkeypatch):
    monkeypatch.setattr(system_info, "_gpu_via_nvidia_smi", lambda: [])
    monkeypatch.setattr(system_info, "_gpu_via_pynvml", lambda: [])
    snap = system_info._gpu_snapshot()
    assert snap["available"] is False
    assert "reason" in snap


def test_gpu_snapshot_uses_nvidia_smi_and_processes(monkeypatch):
    monkeypatch.setattr(system_info, "_gpu_via_nvidia_smi",
                        lambda: [{"index": 0, "name": "RTX 3080",
                                  "memory_used_mib": 645}])
    monkeypatch.setattr(system_info, "_gpu_processes_via_nvidia_smi",
                        lambda: [{"pid": 1, "name": "x", "memory_used_mib": 10}])
    monkeypatch.setattr(system_info, "_gpu_via_pynvml", lambda: [])
    snap = system_info._gpu_snapshot()
    assert snap["available"] is True
    assert snap["source"] == "nvidia-smi"
    assert snap["gpus"][0]["name"] == "RTX 3080"
    assert snap["processes"][0]["pid"] == 1


# ---------------------------------------------------------------- tool shape ---

async def test_system_info_returns_host_sections(ctx):
    r = await REGISTRY.call("system.info", ctx)
    assert r.ok, r.error
    for key in ("hostname", "os", "cpu", "memory", "disks", "gpu", "boot_time"):
        assert key in r.data
    assert "available" in r.data["gpu"]


async def test_system_metrics_returns_gpu_and_battery(ctx):
    r = await REGISTRY.call("system.metrics", ctx)
    assert r.ok, r.error
    for key in ("cpu", "memory", "disks", "network", "gpu", "battery", "temperatures"):
        assert key in r.data
    assert "available" in r.data["gpu"]
