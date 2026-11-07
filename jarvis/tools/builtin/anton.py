"""Anton bridge tools for JARVIS.

Anton is the patient infrastructure node: a job queue, an object store, a
search index and a cross-platform action coordinator. These tools expose it to
Jarvis without giving the model a shell.

Configuration (first match wins):
  * ``ANTON_URL`` environment variable (e.g. http://<ANTON_HOST>:8787)
  * ``config.anton.url`` if a future Jarvis config section exists
  * ``http://127.0.0.1:8787`` then ``http://<ANTON_HOST>:8787``

Token (first match wins):
  * ``ctx.secrets("ANTON_TOKEN")``
  * ``ANTON_TOKEN`` environment variable
  * ``ANTON_TOKEN_FILE``
  * ``~/.config/anton/token``
  * ``<jarvis>/data/anton-token``

The token is never returned in a tool result or logged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:  # Jarvis declares httpx; keep the module importable without it.
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.anton")

__all__ = [
    "AntonStatusTool", "AntonIntentTool", "AntonJobsTool", "AntonNodesTool",
    "AntonSubmitTool", "AntonActionTool", "AntonGoalTool", "AntonApproveTool",
    "AntonRoutinesTool", "AntonRoutineAddTool", "AntonRoutineRunTool",
    "AntonTrainTool",
]


def _config_url(ctx: ToolContext) -> str:
    url = os.environ.get("ANTON_URL", "").strip()
    if url:
        return url.rstrip("/")
    cfg = getattr(ctx, "config", None)
    anton = getattr(cfg, "anton", None) if cfg is not None else None
    if anton is not None:
        value = getattr(anton, "url", "") or ""
        if value:
            return str(value).rstrip("/")
    return ""


def _token(ctx: ToolContext) -> str:
    try:
        getter = getattr(ctx, "secrets", None)
        if callable(getter):
            value = getter("ANTON_TOKEN") or getter("anton_token")
            if value:
                return str(value).strip()
    except Exception:
        pass
    value = os.environ.get("ANTON_TOKEN", "").strip()
    if value:
        return value
    candidates: list[Path] = []
    explicit = os.environ.get("ANTON_TOKEN_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.home() / ".config" / "anton" / "token")
    candidates.append(Path(__file__).resolve().parents[3] / "data" / "anton-token")
    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return ""


class _Client:
    def __init__(self, ctx: ToolContext) -> None:
        if httpx is None:
            raise RuntimeError("httpx is not installed in the Jarvis environment")
        configured = _config_url(ctx)
        self.urls = [configured] if configured else [
            "http://127.0.0.1:8787",
            "http://<ANTON_HOST>:8787",
        ]
        self.token = _token(ctx)

    async def request(self, method: str, path: str, **kwargs: Any):
        if not self.token:
            raise RuntimeError(
                "no Anton token: set ANTON_TOKEN / ANTON_TOKEN_FILE "
                "or place it in ~/.config/anton/token")
        headers = {"Authorization": f"Bearer {self.token}"}
        last_error: Exception | None = None
        for base in self.urls:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.request(method, base + path,
                                                    headers=headers, **kwargs)
                try:
                    data = response.json()
                except Exception:
                    data = {"raw": response.text[:2000]}
                if response.status_code >= 400:
                    error = (data.get("error") if isinstance(data, dict) else None) \
                        or f"HTTP {response.status_code}"
                    raise RuntimeError(str(error))
                return data
            except httpx.ConnectError as exc:  # try the next base URL
                last_error = exc
                continue
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Anton request failed: {exc}") from exc
        raise RuntimeError(f"Anton unreachable at {', '.join(self.urls)}: {last_error}")


def _job_evidence(data: dict) -> dict[str, Any]:
    job = data.get("job") if isinstance(data.get("job"), dict) else data
    if not isinstance(job, dict):
        return {}
    return {"job_id": job.get("id"), "state": job.get("state"),
            "message": job.get("message")}


class _Base(BaseTool):
    async def _call(self, ctx: ToolContext, method: str, path: str,
                    **kwargs: Any) -> ToolResult:
        try:
            client = _Client(ctx)
            data = await client.request(method, path, **kwargs)
            return ToolResult(ok=True, data=data,
                              evidence={"method": "anton-http", "path": path,
                                        **_job_evidence(data)},
                              certainty="unverified" if method == "POST" else "settled")
        except Exception as exc:
            log.warning("anton call %s %s failed: %s", method, path, exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified" if method == "POST" else "unknown")

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if not result.ok:
            return None
        evidence = result.evidence or {}
        job_id = evidence.get("job_id")
        if not job_id:
            return {"verified": True, "method": "read-only"}
        try:
            client = _Client(ctx)
            job = await client.request("GET", f"/jobs/{job_id}")
            state = job.get("state")
            return {"verified": state in ("queued", "running", "dispatched",
                                          "waiting_approval", "done"),
                    "method": "anton-job-readback", "job_id": job_id, "state": state}
        except Exception as exc:
            return {"verified": False, "method": "anton-job-readback",
                    "error": str(exc)}


@register(["anton.status", "anton.state"])
class AntonStatusTool(_Base):
    spec = ToolSpec(
        name="anton.status",
        summary="Anton queue, nodes, objects and scheduler state.",
        version="1.0", category="anton", side_effect=SideEffect.NONE,
        params=(), returns="object: Anton /status payload",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        # The intent endpoint returns a compact status; /status includes full job
        # payloads that are needlessly large for a model context.
        return await self._call(ctx, "POST", "/intent",
                                json={"text": "status", "source": "jarvis"})


@register(["anton.jobs"])
class AntonJobsTool(_Base):
    spec = ToolSpec(
        name="anton.jobs",
        summary="List recent Anton jobs, optionally filtered by state.",
        version="1.0", category="anton", side_effect=SideEffect.NONE,
        params=(
            ToolParam("state", "string", "queued, running, done, failed, canceled."),
            ToolParam("limit", "integer", "Maximum number of jobs.", default=20),
        ),
        returns="object: {jobs}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        limit = max(1, min(200, int(kwargs.get("limit") or 20)))
        state = str(kwargs.get("state") or "").strip()
        path = f"/jobs?limit={limit}" + (f"&state={state}" if state else "")
        return await self._call(ctx, "GET", path)


@register(["anton.nodes"])
class AntonNodesTool(_Base):
    spec = ToolSpec(
        name="anton.nodes",
        summary="List machines enrolled in Anton and their state.",
        version="1.0", category="anton", side_effect=SideEffect.NONE,
        params=(), returns="object: {nodes, stats}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        return await self._call(ctx, "GET", "/nodes")


@register(["anton.intent", "anton.run"])
class AntonIntentTool(_Base):
    spec = ToolSpec(
        name="anton.intent",
        summary=("Ask Anton to do something in one sentence: status, hash, index, "
                 "dedupe, compress, download, ping, inventory, update systems, "
                 "search, approve, calculate."),
        version="1.0", category="anton", side_effect=SideEffect.REMOTE,
        params=(
            ToolParam("text", "string", "Natural-language intent for Anton.", required=True),
            ToolParam("variables", "object", "Numeric variables for calculate."),
            ToolParam("priority", "integer", "0..4; default 2.", default=2),
        ),
        returns="object: {ok, intent, job|result, message}",
        needs_network=True, cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        text = str(kwargs.get("text") or "").strip()
        if not text:
            return ToolResult(ok=False, error="text is required")
        body = {"text": text, "source": "jarvis",
                "variables": kwargs.get("variables") or {},
                "priority": int(kwargs.get("priority") or 2)}
        return await self._call(ctx, "POST", "/intent", json=body)


@register(["anton.submit"])
class AntonSubmitTool(_Base):
    spec = ToolSpec(
        name="anton.submit",
        summary="Submit an explicit Anton job type with parameters.",
        version="1.0", category="anton", side_effect=SideEffect.REMOTE,
        params=(
            ToolParam("type", "string", "Job type (hash, index, dedupe, ...).", required=True),
            ToolParam("params", "object", "Job parameters.", required=True),
            ToolParam("priority", "integer", "0..4; default 2.", default=2),
        ),
        returns="object: job", needs_network=True, cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        type_ = str(kwargs.get("type") or "").strip()
        params = kwargs.get("params")
        if not type_ or not isinstance(params, dict):
            return ToolResult(ok=False, error="type and params are required")
        body = {"type": type_, "params": params,
                "priority": int(kwargs.get("priority") or 2)}
        return await self._call(ctx, "POST", "/jobs", json=body)


@register(["anton.action"])
class AntonActionTool(_Base):
    spec = ToolSpec(
        name="anton.action",
        summary="Route a named action (PING, COLLECT, SYSTEM_UPDATE, ...) to a node.",
        version="1.0", category="anton", side_effect=SideEffect.REMOTE,
        params=(
            ToolParam("action", "string", "Action name.", required=True),
            ToolParam("target", "string", "Node id/name or auto.", default="auto"),
            ToolParam("dry_run", "boolean", "Preview instead of applying.", default=True),
        ),
        returns="object: job", needs_network=True, cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action") or "").strip().upper()
        if not action:
            return ToolResult(ok=False, error="action is required")
        body = {"type": "action", "params": {
            "action": action,
            "target": str(kwargs.get("target") or "auto"),
            "dry_run": bool(kwargs.get("dry_run", True)),
        }, "priority": 2}
        return await self._call(ctx, "POST", "/jobs", json=body)


@register(["anton.goal"])
class AntonGoalTool(_Base):
    spec = ToolSpec(
        name="anton.goal",
        summary="Start an Anton goal: FLEET_INVENTORY or MAINTAIN_FLEET.",
        version="1.0", category="anton", side_effect=SideEffect.REMOTE,
        params=(ToolParam("name", "string", "Goal name.", required=True),),
        returns="object: goal", needs_network=True, cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        name = str(kwargs.get("name") or "").strip().upper()
        if not name:
            return ToolResult(ok=False, error="name is required")
        return await self._call(ctx, "POST", "/goals", json={"name": name})


@register(["anton.approve"])
class AntonApproveTool(_Base):
    spec = ToolSpec(
        name="anton.approve",
        summary="Approve or reject a pending Anton approval.",
        version="1.0", category="anton", side_effect=SideEffect.REMOTE,
        params=(
            ToolParam("id", "integer", "Approval id.", required=True),
            ToolParam("approve", "boolean", "True to approve, false to reject.", default=True),
        ),
        returns="object: decision", needs_network=True, cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            approval_id = int(kwargs.get("id"))
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="id must be an integer")
        approve = bool(kwargs.get("approve", True))
        return await self._call(ctx, "POST",
                                f"/approvals/{approval_id}/"
                                f"{'approve' if approve else 'reject'}",
                                json={"note": "via Jarvis"})


@register(["anton.routines"])
class AntonRoutinesTool(_Base):
    spec = ToolSpec(
        name="anton.routines",
        summary="List Anton scheduled routines.",
        version="1.0", category="anton", side_effect=SideEffect.NONE,
        params=(), returns="object: {routines}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        return await self._call(ctx, "GET", "/routines")


@register(["anton.routine.add"])
class AntonRoutineAddTool(_Base):
    spec = ToolSpec(
        name="anton.routine.add",
        summary="Create or update a recurring Anton intent.",
        version="1.0", category="anton", side_effect=SideEffect.REMOTE,
        params=(
            ToolParam("name", "string", "Routine name.", required=True),
            ToolParam("text", "string", "Intent to run.", required=True),
            ToolParam("interval_s", "integer", "Seconds between runs.", default=86400),
            ToolParam("enabled", "boolean", "Whether it runs.", default=True),
        ),
        returns="object: routine", needs_network=True, cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        name = str(kwargs.get("name") or "").strip()
        text = str(kwargs.get("text") or "").strip()
        if not name or not text:
            return ToolResult(ok=False, error="name and text are required")
        body = {"name": name, "text": text,
                "interval_s": int(kwargs.get("interval_s") or 86400),
                "enabled": bool(kwargs.get("enabled", True)),
                "origin": "jarvis"}
        return await self._call(ctx, "POST", "/routines", json=body)


@register(["anton.routine.run"])
class AntonRoutineRunTool(_Base):
    spec = ToolSpec(
        name="anton.routine.run",
        summary="Run an Anton routine immediately.",
        version="1.0", category="anton", side_effect=SideEffect.REMOTE,
        params=(ToolParam("name", "string", "Routine name.", required=True),),
        returns="object: intent result", needs_network=True, cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        name = str(kwargs.get("name") or "").strip()
        if not name:
            return ToolResult(ok=False, error="name is required")
        return await self._call(ctx, "POST", f"/routines/{name}/run")


@register(["anton.train"])
class AntonTrainTool(_Base):
    spec = ToolSpec(
        name="anton.train",
        summary=("Teach Anton a corpus by indexing and fingerprinting it. "
                 "This is not LLM training; it builds searchable memory."),
        version="1.0", category="anton", side_effect=SideEffect.REMOTE,
        params=(ToolParam("path", "string", "File or folder to ingest.", required=True),),
        returns="object: job", needs_network=True, cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path is required")
        body = {"text": f"train anton on {path}", "source": "jarvis", "priority": 3}
        return await self._call(ctx, "POST", "/intent", json=body)
