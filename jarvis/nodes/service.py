"""Core-side node (satellite) service.

``NodeService`` is the Core's single window onto the distributed ThinkPad
satellites. It:

  * resolves and provisions the shared secret (``NodesConfig.token`` or a
    one-time generated value persisted in the ``kv`` table under ``node_token``);
  * dispatches tasks to a satellite (``POST /node/task``) with typed errors;
  * health-checks a satellite (``GET /node/health``);
  * lists what is recorded in the ``nodes`` table without inventing
    reachability;
  * runs a cancellable background heartbeat that pings enrolled nodes, updates
    ``last_seen``/``status`` and publishes a bus event only when the state
    actually changes.

It is constructed by the kernel as ``NodeService(jarvis)`` and started/stopped
with the rest of the optional subsystems. Nothing here ever logs the token.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..ids import new_id, node_id as new_node_id, now_iso
from ..logging_setup import get_logger
from .protocol import (
    DEFAULT_NODE_PORT,
    HEADER,
    HEALTH_PATH,
    HEALTH_TIMEOUT_S,
    HEARTBEAT_INTERVAL_S,
    TASK_PATH,
    TASK_TIMEOUT_S,
    ErrorCode,
)

try:  # optional at import time; the Node does not need httpx to exist.
    import httpx
except Exception:  # pragma: no cover - only when the dep is absent
    httpx = None  # type: ignore[assignment]

log = get_logger("jarvis.nodes.service")

__all__ = [
    "NodeService",
    "NodeError",
    "NodeUnreachable",
    "NodeTimeout",
    "NodeAuthError",
    "NodeProtocolError",
]


# ---------------------------------------------------------------------------
# typed errors
# ---------------------------------------------------------------------------


class NodeError(Exception):
    """Base class for node-link failures."""

    code: ErrorCode = ErrorCode.INTERNAL


class NodeUnreachable(NodeError):
    """The satellite could not be contacted at all."""

    code = ErrorCode.UNREACHABLE


class NodeTimeout(NodeError):
    """The satellite did not answer within the budget."""

    code = ErrorCode.TIMEOUT


class NodeAuthError(NodeError):
    """The satellite rejected our shared token (HTTP 401)."""

    code = ErrorCode.UNAUTHORIZED


class NodeProtocolError(NodeError):
    """The satellite answered, but not with a usable response."""

    code = ErrorCode.INTERNAL


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class NodeService:
    """Distributed satellite manager (Core side)."""

    def __init__(self, jarvis: Any) -> None:
        self.jarvis = jarvis
        self.cfg = jarvis.config
        self.db = jarvis.db
        self.bus = getattr(jarvis, "bus", None)

        self.heartbeat_interval_s = HEARTBEAT_INTERVAL_S
        self.health_timeout_s = HEALTH_TIMEOUT_S
        self.task_timeout_s = TASK_TIMEOUT_S

        self._client: Any = None
        self._task: asyncio.Task[Any] | None = None
        self._stop_event: asyncio.Event | None = None
        self._token: str | None = None
        self._last_state: dict[str, str] = {}

    # -- shared secret ---------------------------------------------------
    def token(self) -> str:
        """Return the shared secret, generating and persisting it once if needed.

        The generated value is stored in the ``kv`` table under ``node_token``
        so a restart reuses the same secret. The value is never logged.
        """
        if self._token:
            return self._token
        configured = str(getattr(self.cfg.nodes, "token", "") or "").strip()
        if configured:
            self._token = configured
            return self._token
        try:
            existing = self.db.kv_get("node_token")
        except Exception:  # pragma: no cover - defensive
            existing = None
        if existing:
            self._token = str(existing)
            return self._token
        generated = new_id("nt")
        try:
            self.db.kv_set("node_token", generated)
            self.db.audit("nodes.token.generate", actor="jarvis",
                          detail={"persisted": "kv:node_token"})
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("could not persist generated node token: %s", exc)
        self._token = generated
        return self._token

    # -- http plumbing ---------------------------------------------------
    def _ensure_client(self) -> Any:
        if httpx is None:
            raise NodeError("httpx is required on the Core to talk to satellites")
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.health_timeout_s),
                headers={"User-Agent": "jarvis-core/nodes"},
            )
        return self._client

    def _base_url(self, node_id: str) -> str | None:
        """Resolve a node's base URL from the table, then from config."""
        row = self.db.query_one("SELECT base_url FROM nodes WHERE id=?", (node_id,))
        if row and row["base_url"]:
            return str(row["base_url"]).rstrip("/")
        known = getattr(self.cfg.nodes, "known", {}) or {}
        url = known.get(node_id)
        return str(url).rstrip("/") if url else None

    def _record_state(self, node_id: str, status: str, *, latency_ms: float | None = None,
                      capabilities: list[str] | None = None) -> None:
        """Persist observed state and publish only on an actual transition."""
        ts = now_iso()
        try:
            if status == "online":
                self.db.execute(
                    "UPDATE nodes SET status=?, last_seen=? WHERE id=?",
                    (status, ts, node_id),
                )
            else:
                self.db.execute("UPDATE nodes SET status=? WHERE id=?", (status, node_id))
            if capabilities is not None:
                self.db.execute(
                    "UPDATE nodes SET capabilities=? WHERE id=?",
                    (json.dumps(capabilities, ensure_ascii=False), node_id),
                )
        except Exception as exc:  # pragma: no cover - db should not break the loop
            log.warning("could not record state for %s: %s", node_id, exc)

        if self._last_state.get(node_id) == status:
            return
        self._last_state[node_id] = status
        if self.bus is not None:
            try:
                self.bus.publish("node.state", node_id=node_id, state=status,
                                 latency_ms=latency_ms,
                                 last_seen=ts if status == "online" else None)
            except Exception:  # pragma: no cover - observers must not break us
                pass

    # -- public API ------------------------------------------------------
    async def list_nodes(self) -> list[dict[str, Any]]:
        """Return the rows recorded in the ``nodes`` table.

        This reflects *last known* status only; it never probes and never
        invents reachability. Configured-but-unenrolled satellites are not
        fabricated here either -- they appear once ``enroll`` records them.
        """
        rows = self.db.query("SELECT * FROM nodes ORDER BY name, id")
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            caps = item.get("capabilities")
            if isinstance(caps, str) and caps:
                try:
                    item["capabilities"] = json.loads(caps)
                except (TypeError, ValueError):
                    item["capabilities"] = []
            elif not isinstance(caps, list):
                item["capabilities"] = []
            out.append(item)
        return out

    async def ping(self, node_id: str) -> dict[str, Any]:
        """Health-check one node; never raises, always returns a dict."""
        base = self._base_url(node_id)
        if not base:
            self._record_state(node_id, "unknown")
            return {"ok": False, "node_id": node_id, "latency_ms": None,
                    "error": "no base_url on record for this node",
                    "code": ErrorCode.NOT_FOUND.value}
        url = base + HEALTH_PATH
        started = time.perf_counter()
        try:
            client = self._ensure_client()
            resp = await client.get(url, headers={HEADER: self.token()})
        except Exception as exc:
            latency = round((time.perf_counter() - started) * 1000.0, 2)
            self._record_state(node_id, "unreachable", latency_ms=latency)
            return {"ok": False, "node_id": node_id, "url": url, "latency_ms": latency,
                    "error": _short(exc), "code": _code_of(exc)}
        latency = round((time.perf_counter() - started) * 1000.0, 2)
        if resp.status_code == 401:
            self._record_state(node_id, "unauthorized", latency_ms=latency)
            return {"ok": False, "node_id": node_id, "url": url, "latency_ms": latency,
                    "error": "node rejected the shared token",
                    "code": ErrorCode.UNAUTHORIZED.value}
        if resp.status_code >= 400:
            self._record_state(node_id, "error", latency_ms=latency)
            return {"ok": False, "node_id": node_id, "url": url, "latency_ms": latency,
                    "error": f"HTTP {resp.status_code}", "code": ErrorCode.INTERNAL.value}
        try:
            node = resp.json()
        except Exception:
            self._record_state(node_id, "error", latency_ms=latency)
            return {"ok": False, "node_id": node_id, "url": url, "latency_ms": latency,
                    "error": "node returned non-JSON health", "code": ErrorCode.INTERNAL.value}

        caps = node.get("capabilities") if isinstance(node, dict) else None
        self._record_state(node_id, "online", latency_ms=latency,
                           capabilities=caps if isinstance(caps, list) else None)
        return {"ok": True, "node_id": node_id, "url": url,
                "latency_ms": latency, "node": node}

    async def send_task(self, node_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a task to a node and return its JSON response.

        Raises a typed :class:`NodeError` subclass on transport/auth/protocol
        failure. A node that answers with ``{"ok": false}`` is *not* an
        exception -- that is a normal application-level result.
        """
        base = self._base_url(node_id)
        if not base:
            raise NodeUnreachable(f"no base_url on record for node '{node_id}'")
        url = base + TASK_PATH
        task_id = new_id("ntask")
        body = {"kind": str(kind), "payload": dict(payload or {}), "task_id": task_id}

        started = time.perf_counter()
        try:
            client = self._ensure_client()
            resp = await client.post(
                url, json=body, headers={HEADER: self.token()},
                timeout=httpx.Timeout(self.task_timeout_s) if httpx else None,
            )
        except Exception as exc:
            latency = round((time.perf_counter() - started) * 1000.0, 2)
            self._record_state(node_id, "unreachable", latency_ms=latency)
            if isinstance(exc, getattr(httpx, "TimeoutException", ())):
                raise NodeTimeout(f"task {kind} to '{node_id}' timed out") from exc
            raise NodeUnreachable(f"task {kind} to '{node_id}' failed: {_short(exc)}") from exc

        latency = round((time.perf_counter() - started) * 1000.0, 2)
        if resp.status_code == 401:
            self._record_state(node_id, "unauthorized", latency_ms=latency)
            raise NodeAuthError(f"node '{node_id}' rejected the shared token")
        if resp.status_code >= 400:
            self._record_state(node_id, "error", latency_ms=latency)
            raise NodeProtocolError(f"node '{node_id}' returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception as exc:
            self._record_state(node_id, "error", latency_ms=latency)
            raise NodeProtocolError(f"node '{node_id}' returned non-JSON body") from exc

        self._record_state(node_id, "online", latency_ms=latency)
        if isinstance(data, dict):
            data.setdefault("task_id", task_id)
            data.setdefault("latency_ms", latency)
            return data
        return {"ok": False, "code": ErrorCode.INTERNAL.value,
                "error": "node returned a non-object JSON body",
                "task_id": task_id, "latency_ms": latency, "raw": data}

    def enroll(self, name: str, base_url: str, capabilities: list[str] | None = None) -> dict[str, Any]:
        """Insert or update a satellite row with status ``enrolled``.

        Enrolling records *intent to trust*; it does not claim the node is
        reachable. Call :meth:`ping` to observe that.
        """
        name = str(name).strip() or "node"
        base_url = str(base_url).strip().rstrip("/")
        caps = list(capabilities or [])
        caps_json = json.dumps(caps, ensure_ascii=False)

        row = self.db.query_one(
            "SELECT id FROM nodes WHERE base_url=? OR name=? ORDER BY first_seen LIMIT 1",
            (base_url, name),
        )
        ts = now_iso()
        if row:
            nid = row["id"]
            self.db.execute(
                "UPDATE nodes SET name=?, base_url=?, capabilities=?, status='enrolled' "
                "WHERE id=?",
                (name, base_url, caps_json, nid),
            )
        else:
            nid = new_node_id()
            self.db.execute(
                "INSERT INTO nodes(id,name,base_url,kind,first_seen,capabilities,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (nid, name, base_url, "node", ts, caps_json, "enrolled"),
            )
        self.db.audit("nodes.enroll", actor="owner", target=nid,
                      detail={"name": name, "base_url": base_url})
        if self.bus is not None:
            try:
                self.bus.publish("node.state", node_id=nid, state="enrolled",
                                 name=name, base_url=base_url)
            except Exception:  # pragma: no cover
                pass
        created = self.db.query_one("SELECT * FROM nodes WHERE id=?", (nid,))
        out = dict(created) if created else {"id": nid, "name": name, "base_url": base_url,
                                             "status": "enrolled"}
        if isinstance(out.get("capabilities"), str):
            try:
                out["capabilities"] = json.loads(out["capabilities"])
            except (TypeError, ValueError):
                out["capabilities"] = []
        return out

    def status(self) -> dict[str, Any]:
        """Cheap, synchronous snapshot for health/UI endpoints."""
        try:
            known = len(getattr(self.cfg.nodes, "known", {}) or {})
        except Exception:  # pragma: no cover
            known = 0
        try:
            enrolled = self.db.query_one("SELECT COUNT(*) AS n FROM nodes")
            enrolled_n = int(enrolled["n"]) if enrolled else 0
        except Exception:  # pragma: no cover
            enrolled_n = 0
        running = self._task is not None and not self._task.done()
        return {
            "enabled": bool(getattr(self.cfg.nodes, "enabled", True)),
            "running": running,
            "known": known,
            "enrolled": enrolled_n,
            "heartbeat_interval_s": self.heartbeat_interval_s,
            "node_port": int(getattr(self.cfg.server, "node_port", DEFAULT_NODE_PORT)),
        }

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Launch the background heartbeat loop (never raises into the kernel)."""
        if not bool(getattr(self.cfg.nodes, "enabled", True)):
            log.info("nodes subsystem disabled; heartbeat not started")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        try:
            self._task = asyncio.create_task(self._heartbeat_loop(), name="jarvis-nodes-heartbeat")
        except Exception as exc:  # pragma: no cover - defensive
            log.error("could not start nodes heartbeat: %s", exc)
            self._task = None
            return
        log.info("nodes heartbeat started (every %.0fs)", self.heartbeat_interval_s)

    async def stop(self) -> None:
        """Cancel the heartbeat loop and close the HTTP client."""
        if self._stop_event is not None:
            self._stop_event.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # pragma: no cover
                log.debug("error closing node http client: %s", exc)
        log.info("nodes heartbeat stopped")

    # -- heartbeat -------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        try:
            while not (self._stop_event and self._stop_event.is_set()):
                try:
                    await self._heartbeat_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # never let one cycle kill the loop
                    log.warning("nodes heartbeat cycle failed: %s", exc)
                try:
                    assert self._stop_event is not None
                    await asyncio.wait_for(self._stop_event.wait(),
                                           timeout=self.heartbeat_interval_s)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    async def _heartbeat_once(self) -> None:
        try:
            rows = self.db.query("SELECT id FROM nodes")
        except Exception as exc:  # pragma: no cover
            log.warning("cannot read nodes for heartbeat: %s", exc)
            return
        for row in rows:
            nid = row["id"]
            try:
                await self.ping(nid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # ping already swallows most; belt and braces
                log.warning("heartbeat ping of %s failed: %s", nid, exc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _short(exc: Exception, limit: int = 200) -> str:
    return f"{type(exc).__name__}: {exc}"[:limit]


def _code_of(exc: Exception) -> str:
    if httpx is not None and isinstance(exc, getattr(httpx, "TimeoutException", ())):
        return ErrorCode.TIMEOUT.value
    return ErrorCode.UNREACHABLE.value
