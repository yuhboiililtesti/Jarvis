"""OPTIONAL defensive decoy ("honeypot").

This is deliberately small and deliberately honest. When enabled it opens a
single TCP listener on a port that nothing legitimate should use, and it records
every connection attempt as an alert. That is *all* it does:

  * it does **not** trap, block, tarpit, fingerprint or attribute an attacker;
  * it never claims those things in an alert, a log line or its status;
  * it defaults to **disabled** — nothing listens until the owner opts in.

Enable it explicitly with ``JARVIS_HONEYPOT=1`` (and optionally
``JARVIS_HONEYPOT_PORT`` / ``JARVIS_HONEYPOT_HOST``) or by passing ``enabled=True``.
The default port is 2222.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from ..ids import now_iso
from ..logging_setup import get_logger
from ..monitoring import alerts as alerts_mod

log = get_logger("jarvis.security.honeypot")

__all__ = ["Honeypot"]

DEFAULT_PORT = 2222
DEFAULT_HOST = "0.0.0.0"
MAX_READ = 1024


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class Honeypot:
    def __init__(self, jarvis: Any, *, enabled: bool | None = None,
                 port: int | None = None, host: str | None = None) -> None:
        self.j = jarvis
        self.db = getattr(jarvis, "db", None)
        self.bus = getattr(jarvis, "bus", None)
        self.enabled = _truthy(os.environ.get("JARVIS_HONEYPOT")) if enabled is None else bool(enabled)
        if port is None:
            try:
                port = int(os.environ.get("JARVIS_HONEYPOT_PORT", str(DEFAULT_PORT)))
            except ValueError:
                port = DEFAULT_PORT
        self.port = int(port)
        self.host = host or os.environ.get("JARVIS_HONEYPOT_HOST", DEFAULT_HOST)

        self._server: asyncio.Server | None = None
        self._running = False
        self._attempts = 0
        self._last_attempt_at: str | None = None

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled:
            log.info("honeypot disabled (opt in with JARVIS_HONEYPOT=1)")
            return
        if self._running:
            return
        try:
            self._server = await asyncio.start_server(
                self._handle, host=self.host, port=self.port)
        except OSError as exc:
            log.warning("honeypot could not bind %s:%s: %s", self.host, self.port, exc)
            self.bus and self.bus.degrade("security.honeypot",
                                          f"could not bind {self.host}:{self.port}: {exc}")
            self._server = None
            return
        self._running = True
        self.bus and self.bus.status("security.honeypot", "listening",
                                     host=self.host, port=self.port,
                                     note="records connection attempts only; does not trap")
        log.info("honeypot listening on %s:%s (recording attempts only)", self.host, self.port)

    async def stop(self) -> None:
        self._running = False
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # pragma: no cover
                pass
            self._server = None
        self.bus and self.bus.status("security.honeypot", "stopped")

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        remote_ip, remote_port = (peer[0], peer[1]) if isinstance(peer, tuple) and len(peer) >= 2 \
            else ("unknown", 0)
        preview = b""
        try:
            preview = await asyncio.wait_for(reader.read(MAX_READ), timeout=1.5)
        except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError):
            preview = b""
        except Exception:  # pragma: no cover - defensive
            preview = b""
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:  # pragma: no cover
                pass

        self._attempts += 1
        self._last_attempt_at = now_iso()
        evidence = {
            "remote_ip": remote_ip,
            "remote_port": remote_port,
            "local_port": self.port,
            "bytes_received": len(preview),
            "preview_hex": preview[:64].hex() if preview else "",
            "recorded_at": self._last_attempt_at,
            "note": "connection attempt recorded; no trapping or attribution performed",
        }
        try:
            alerts_mod.create_alert(
                self.db, self.bus, "warn", "security.honeypot",
                f"Decoy port {self.port} connection attempt",
                f"Connection from {remote_ip}:{remote_port} to decoy port {self.port}. "
                "Recorded only — the decoy does not trap or identify anything.",
                evidence)
        except Exception as exc:  # pragma: no cover
            log.warning("honeypot alert failed: %s", exc)
        try:
            self.db.audit("security.honeypot.attempt",
                          detail={"remote_ip": remote_ip, "remote_port": remote_port},
                          severity="warn")
            self.db.kv_set("security.honeypot.attempts", self._attempts)
        except Exception:  # pragma: no cover
            pass
        if self.bus is not None:
            try:
                self.bus.publish("honeypot.attempt", remote_ip=remote_ip,
                                 remote_port=remote_port, port=self.port)
            except Exception:  # pragma: no cover
                pass

    # -- status ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "host": self.host,
            "port": self.port,
            "attempts": self._attempts,
            "last_attempt_at": self._last_attempt_at,
            "capability": "records connection attempts only",
            "traps_traffic": False,
        }
