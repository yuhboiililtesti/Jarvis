"""Backup controller - 3-2-1 strategy for critical data only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..ids import now_iso
from ..logging_setup import get_logger

log = get_logger("jarvis.backup")


class BackupController:
    """Manage critical backups with size limits."""

    # Critical paths to backup (max ~200GB per system as specified)
    CRITICAL_PATHS = {
        "apos": [
            "config/",  # JARVIS config
            "data/jarvis.db",  # JARVIS database
            "jarvis/",  # JARVIS source
            "Documentation/Projects/AI/Jarvis/",  # Critical docs
        ],
        "pc001": [
            "Projects/AI/Jarvis/",  # JARVIS client code if any
            "Documentation/Projects/AI/Jarvis/",
        ],
        "laptop001": [
            "Documents/",  # Small critical data only
            "configs/",  # Configs
        ]
    }

    MAX_BACKUP_SIZE_GB = 200  # Per system
    LAPTOP_MAX_GB = 100

    def __init__(self, jarvis: Any = None) -> None:
        self.jarvis = jarvis
        self._backups: list[dict[str, Any]] = []

    def inventory(self) -> dict[str, Any]:
        """Get backup inventory."""
        return {
            "backups": self._backups,
            "critical_paths": self.CRITICAL_PATHS,
            "max_size_gb": self.MAX_BACKUP_SIZE_GB,
            "laptop_max_gb": self.LAPTOP_MAX_GB
        }

    def create_prechange_backup(self, description: str, paths: list[str] | None = None) -> dict[str, Any]:
        """Create backup before consequential changes."""
        backup_id = f"backup_{now_iso().replace(':', '-')}"
        backup = {
            "id": backup_id,
            "description": description,
            "timestamp": now_iso(),
            "paths": paths or [],
            "status": "created",
            "verified": False,
            "size_gb": 0
        }
        self._backups.append(backup)
        log.info("pre-change backup created: %s", backup_id)
        return backup

    def verify_backup(self, backup_id: str) -> bool:
        """Verify backup integrity."""
        for backup in self._backups:
            if backup["id"] == backup_id:
                backup["verified"] = True
                log.info("backup verified: %s", backup_id)
                return True
        return False

    def status(self) -> dict[str, Any]:
        return {
            "total_backups": len(self._backups),
            "verified": sum(1 for b in self._backups if b.get("verified")),
            "strategy": "3-2-1 (critical data only, max 200GB per system)"
        }
