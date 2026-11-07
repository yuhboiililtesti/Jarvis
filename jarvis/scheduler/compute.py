"""Hardware-aware compute scheduler."""

from __future__ import annotations

from typing import Any


class ComputeScheduler:
    def __init__(self, jarvis=None):
        self.jarvis = jarvis

    def discover_hardware(self):
        return {
            "pc001": {"gpu": "RTX 3080 10GB", "role": "gpu_training_rendering"},
            "apos": {"gpu": "GTX 1660 Super 6GB", "role": "core_services"},
            "laptop001": {"gpu": None, "role": "monitoring_fallback"},
        }

    def route_job(self, job_type: str, requirements: dict[str, Any] = None):
        hw = self.discover_hardware()
        if job_type in ["train", "render", "ml_training"]:
            return {"target": "pc001", "method": "local_gpu"}
        if job_type in ["core", "service"]:
            return {"target": "apos", "method": "local"}
        return {"target": "pc001", "method": "default"}


__all__ = ["ComputeScheduler"]
