"""FastAPI application: REST + WebSocket + static HUD for JARVIS.

The API is a thin, honest shell over the kernel. It never invents state: if a
subsystem is unavailable the endpoint says so. The websocket carries the same
events the kernel publishes, so the HUD cannot show activity that is not real.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from ..config import PROJECT_ROOT
from ..core import build
from ..ids import now_iso
from ..logging_setup import get_logger, redact

log = get_logger("jarvis.api")

__all__ = ["create_app", "UI_DIR"]

UI_DIR = PROJECT_ROOT / "jarvis" / "ui"


def create_app(jarvis=None) -> FastAPI:
    kernel = jarvis or build()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await kernel.start()
        app.state.jarvis = kernel
        log.info("api ready on %s:%s", kernel.config.server.host, kernel.config.server.port)
        try:
            yield
        finally:
            await kernel.stop()

    app = FastAPI(title="JARVIS", version="0.1.0", lifespan=lifespan)
    app.state.jarvis = kernel
    app.add_middleware(
        CORSMiddleware,
        allow_origins=kernel.config.server.cors_origins or ["http://127.0.0.1:8765"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added after CORS so it is the outermost layer: nothing remote reaches a
    # route (or a CORS preflight) without passing the private-cloud gate.
    _install_optional_auth(app, kernel)

    # ---------------------------------------------------------------- health
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return await kernel.health()

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        voice = kernel.services.get("voice")
        voice_status: dict[str, Any] = {"available": False, "reason": "voice service not loaded"}
        if voice is not None and hasattr(voice, "status"):
            try:
                voice_status = await _maybe_await(voice.status())
            except Exception as exc:  # pragma: no cover - defensive
                voice_status = {"available": False, "reason": str(exc)}
        return {
            "persona": {
                "name": kernel.persona.name(),
                "mood": kernel.persona.state.mood,
                "busy": kernel.persona.state.busy,
                "address_as": kernel.persona.addr,
            },
            "voice": voice_status,
            "mode": kernel.config.policy.mode,
            "effective_mode": kernel.policy.effective_mode,
            "defcon": kernel.policy.defcon,
            "tools": len(kernel.registry.names()),
            "node_role": kernel.config.host_role,
            "llm": kernel.config.llm.model,
            "ts": now_iso(),
        }

    # ---------------------------------------------------------------- live screen
    screen_url = os.environ.get("JARVIS_SCREEN_LIVE_URL",
                                "http://127.0.0.1:8799")
    screen_token = os.environ.get("JARVIS_SCREEN_TOKEN")

    def _screen_headers() -> dict[str, str]:
        return ({"X-Jarvis-Screen-Token": screen_token}
                if screen_token else {})

    async def _reader_get(path: str, timeout: float = 8.0,
                          params: dict[str, Any] | None = None):
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.get(screen_url.rstrip("/") + path,
                                    params=params, headers=_screen_headers())

    @app.get("/api/screen/health")
    async def screen_health() -> JSONResponse:
        try:
            response = await _reader_get("/health", timeout=4.0)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "capturing": False,
                 "error": f"reader unreachable: {type(exc).__name__}: {exc}"},
                status_code=503)
        return JSONResponse(response.json(), status_code=response.status_code)

    @app.get("/api/screen/latest")
    async def screen_latest() -> JSONResponse:
        try:
            response = await _reader_get("/latest", timeout=6.0)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"reader unreachable: {exc}"},
                status_code=503)
        try:
            payload = response.json()
        except Exception:
            payload = {"ok": False,
                       "error": f"reader returned {response.status_code}"}
        return JSONResponse(payload, status_code=response.status_code)

    @app.get("/api/screen/frame")
    async def screen_frame() -> Response:
        try:
            response = await _reader_get("/frame", timeout=10.0)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"reader unreachable: {exc}"},
                status_code=503)
        if response.status_code != 200:
            try:
                payload = response.json()
            except Exception:
                payload = {"ok": False,
                           "error": f"reader returned {response.status_code}"}
            return JSONResponse(payload, status_code=response.status_code)
        return Response(content=response.content, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/screen/stream")
    async def screen_stream(fps: float = 15.0, scale: int = 1280,
                            quality: int = 55) -> StreamingResponse:
        """Proxy the reader's MJPEG stream (cursor marker included)."""
        async def frames():
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                            "GET", screen_url.rstrip("/") + "/stream.mjpg",
                            params={"fps": fps, "scale": scale,
                                    "quality": quality},
                            headers=_screen_headers()) as response:
                        async for chunk in response.aiter_raw():
                            yield chunk
            except Exception:
                return

        return StreamingResponse(
            frames(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store"})

    @app.get("/api/screen/describe")
    async def screen_describe(q: str = "") -> JSONResponse:
        try:
            async with httpx.AsyncClient(timeout=150.0) as client:
                response = await client.get(
                    screen_url.rstrip("/") + "/describe",
                    params={"q": q} if q else None,
                    headers=_screen_headers())
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"reader unreachable: {exc}"},
                status_code=503)
        try:
            payload = response.json()
        except Exception:
            payload = {"ok": False,
                       "error": f"reader returned {response.status_code}"}
        return JSONResponse(payload, status_code=response.status_code)

    @app.post("/api/screen/capture/{state}")
    async def screen_capture(state: str) -> JSONResponse:
        if state not in ("on", "off"):
            raise HTTPException(status_code=400,
                                detail="state must be 'on' or 'off'")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    screen_url.rstrip("/") + f"/capture/{state}",
                    json={}, headers=_screen_headers())
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"reader unreachable: {exc}"},
                status_code=503)
        return JSONResponse(response.json(), status_code=response.status_code)

    @app.get("/api/evolve/overview")
    async def evolve_overview() -> dict[str, Any]:
        """Evolve's availability, the last JARVIS evolve run, and the Arena."""
        info: dict[str, Any] = {
            "arena": {"url": "http://127.0.0.1:8770/", "ok": False}}
        try:
            import evolve as evolve_pkg  # noqa: PLC0415 - optional dependency
            info["evolve"] = {
                "available": True,
                "version": getattr(evolve_pkg, "__version__", None),
                "path": getattr(evolve_pkg, "__file__", None),
            }
        except Exception as exc:
            info["evolve"] = {"available": False, "reason": str(exc)}
        service = getattr(kernel, "evolve", None)
        try:
            info["last"] = service.status() if service is not None else None
        except Exception as exc:
            info["last"] = {"error": str(exc)}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(info["arena"]["url"])
            info["arena"]["ok"] = response.status_code < 500
            info["arena"]["status"] = response.status_code
        except Exception as exc:
            info["arena"]["error"] = f"{type(exc).__name__}: {exc}"
        return info

    # ---------------------------------------------------------------- websocket
    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        try:
            async for ev in kernel.bus.subscribe():
                await sock.send_text(json.dumps(ev.to_dict(), default=str))
        except WebSocketDisconnect:
            return
        except Exception as exc:  # pragma: no cover
            log.debug("ws closed: %s", exc)
        finally:
            try:
                await sock.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- chat
    @app.post("/api/chat")
    async def chat(payload: dict[str, Any]) -> StreamingResponse:
        message = (payload or {}).get("message", "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message required")
        session_id = (payload or {}).get("session_id") or "default"
        session = kernel.session(session_id)
        # "voice" switches the persona to speech-friendly delivery; anything else is text.
        source = "voice" if (payload or {}).get("source") == "voice" else "text"

        async def gen():
            try:
                async for ev in kernel.agent.run_turn(message, session=session, source=source):
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
            except Exception as exc:  # pragma: no cover
                log.exception("chat failed")
                yield f"data: {json.dumps({'kind': 'error', 'text': str(exc)})}\n\n"
            yield "data: [DONE]\n\n"

        kernel.db.audit("chat", actor="owner", detail={"chars": len(message)})
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/chat/sync")
    async def chat_sync(payload: dict[str, Any]) -> dict[str, Any]:
        message = (payload or {}).get("message", "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message required")
        session = kernel.session((payload or {}).get("session_id") or "default")
        source = "voice" if (payload or {}).get("source") == "voice" else "text"
        text = await kernel.agent.complete(message, session=session, source=source)
        return {"reply": text}

    # ---------------------------------------------------------------- approvals
    @app.get("/api/approvals")
    async def approvals() -> dict[str, Any]:
        return {"pending": kernel.controller.pending_approvals()}

    @app.post("/api/approvals/{approval_id}")
    async def decide(approval_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        decision = (payload or {}).get("decision", "")
        if decision not in ("allow", "deny"):
            raise HTTPException(status_code=400, detail="decision must be allow|deny")
        remember = (payload or {}).get("remember_s")
        remember_session = bool((payload or {}).get("remember_session"))
        ok = kernel.controller.resolve_approval(
            approval_id, decision, by="owner",
            remember_for_s=float(remember) if remember else None,
            remember_session=remember_session)
        if not ok:
            raise HTTPException(status_code=404, detail="approval not found or already resolved")
        return {"ok": True, "decision": decision}

    # ---------------------------------------------------------------- tasks
    @app.get("/api/tasks")
    async def tasks(limit: int = 50) -> dict[str, Any]:
        rows = kernel.db.query(
            "SELECT id,created_at,status,source,request,goal,error FROM tasks "
            "ORDER BY created_at DESC LIMIT ?", (min(limit, 500),))
        return {"tasks": [dict(r) for r in rows]}

    @app.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str) -> dict[str, Any]:
        row = kernel.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        attempts = kernel.db.query(
            "SELECT id,tool,status,side_effect,evidence,error,created_at FROM attempts "
            "WHERE task_id=? ORDER BY created_at", (task_id,))
        out = dict(row)
        out["attempts"] = [dict(a) for a in attempts]
        return out

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str) -> dict[str, Any]:
        return {"cancelled": kernel.controller.cancel(task_id)}

    # ---------------------------------------------------------------- tools
    @app.get("/api/tools")
    async def tools() -> dict[str, Any]:
        cats = kernel.registry.by_category()
        return {
            "count": len(kernel.registry.names()),
            "categories": {k: [s.name for s in v] for k, v in sorted(cats.items())},
            "tools": kernel.registry.describe(),
            "mode": kernel.policy.effective_mode,
        }

    @app.get("/api/tools/health")
    async def tools_health() -> dict[str, Any]:
        health = kernel.registry.health()
        return {"count": len(health),
                "unavailable": sorted(n for n, h in health.items() if not h["available"]),
                "tools": health}

    # ---------------------------------------------------------------- sandbox
    @app.get("/api/sandbox")
    async def sandbox() -> dict[str, Any]:
        svc = kernel.services.get("sandbox")
        if svc is None:
            return {"active_tier": "none", "contained": False,
                    "reason": "sandbox service not loaded"}
        return svc.status()

    # ---------------------------------------------------------------- runtime
    @app.get("/api/runtime")
    async def runtime() -> dict[str, Any]:
        svc = kernel.services.get("runtime")
        if svc is None:
            return {"enabled": False, "running": False,
                    "reason": "runtime service not loaded"}
        return svc.status()

    # Rules are read over HTTP but created only at the console, for the same
    # reason plugins are: a remote client must not be able to define new actions.
    @app.get("/api/runtime/rules")
    async def runtime_rules() -> dict[str, Any]:
        svc = kernel.services.get("runtime")
        if svc is None:
            return {"rules": [], "note": "runtime service not loaded"}
        return {"rules": svc.events.list_rules()}

    @app.get("/api/runtime/events")
    async def runtime_events(limit: int = 50) -> dict[str, Any]:
        svc = kernel.services.get("runtime")
        if svc is None:
            return {"rules": [], "events_recent": [], "reason": "runtime service not loaded"}
        return svc.events.status(limit=limit)

    @app.post("/api/runtime/tick")
    async def runtime_tick(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one bounded tick now (the Diagnostics/Operations button).

        Scheduled jobs are opt-in: a manual tick normally exists to drain queued
        event reactions, and a request that returns quickly must not leave a job
        claimed but unfinished.
        """
        svc = kernel.services.get("runtime")
        if svc is None:
            raise HTTPException(status_code=503, detail="runtime service not loaded")
        jobs = bool((payload or {}).get("jobs", False))
        result = await svc.tick(jobs=jobs)
        await svc.drain(timeout=1.0)
        kernel.db.audit("runtime.tick", actor="owner",
                        detail={"jobs": jobs, "result": result})
        return {"result": result, "status": svc.status()}

    # ---------------------------------------------------------------- goals
    @app.post("/api/goals")
    async def goals_create(payload: dict[str, Any]) -> dict[str, Any]:
        svc = kernel.services.get("goals")
        if svc is None:
            raise HTTPException(status_code=503, detail="goals service not loaded")
        request = str((payload or {}).get("request") or "").strip()
        if not request:
            raise HTTPException(status_code=400, detail="request required")
        if bool((payload or {}).get("dry_run")):
            return {"plan": await svc.plan(request)}
        steps = await svc.plan(request)
        return svc.create(request, steps=steps)

    @app.get("/api/goals")
    async def goals_list(limit: int = 20) -> dict[str, Any]:
        svc = kernel.services.get("goals")
        if svc is None:
            return {"goals": [], "reason": "goals service not loaded"}
        return {"goals": svc.list_goals(limit)}

    @app.get("/api/goals/{goal_id}")
    async def goals_status(goal_id: str) -> dict[str, Any]:
        svc = kernel.services.get("goals")
        if svc is None:
            raise HTTPException(status_code=503, detail="goals service not loaded")
        goal = svc.status(goal_id)
        if goal.get("status") == "missing":
            raise HTTPException(status_code=404, detail="goal not found")
        return goal

    @app.post("/api/goals/{goal_id}/run")
    async def goals_run(goal_id: str) -> dict[str, Any]:
        svc = kernel.services.get("goals")
        if svc is None:
            raise HTTPException(status_code=503, detail="goals service not loaded")
        return await svc.advance(goal_id)

    @app.post("/api/goals/{goal_id}/cancel")
    async def goals_cancel(goal_id: str) -> dict[str, Any]:
        svc = kernel.services.get("goals")
        if svc is None:
            raise HTTPException(status_code=503, detail="goals service not loaded")
        return {"cancelled": svc.cancel(goal_id)}

    # ---------------------------------------------------------------- ops
    @app.get("/api/ops")
    async def ops_status() -> dict[str, Any]:
        svc = kernel.services.get("ops")
        if svc is None:
            return {"ok": None, "checks": [], "reason": "ops service not loaded"}
        return svc.status()

    @app.post("/api/ops/check")
    async def ops_check() -> dict[str, Any]:
        svc = kernel.services.get("ops")
        if svc is None:
            raise HTTPException(status_code=503, detail="ops service not loaded")
        return await svc.check()

    @app.post("/api/events")
    async def ingest_event(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        """Ingest an external event (a sensor, a script, Home Assistant).

        It can only *trigger rules that already exist*, and every reaction still
        passes policy and approval, so this is not an action bypass. A producer
        may send an ``Idempotency-Key`` header (or an ``event_id`` field) so a
        retried delivery does not run the reaction twice.
        """
        svc = kernel.services.get("runtime")
        if svc is None:
            raise HTTPException(status_code=503, detail="runtime service not loaded")
        topic = str((payload or {}).get("topic") or "").strip()
        if not topic:
            raise HTTPException(status_code=400, detail="topic required")
        data = (payload or {}).get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="data must be an object")
        dedupe = (request.headers.get("Idempotency-Key")
                  or (payload or {}).get("event_id") or (payload or {}).get("id"))
        result = svc.events.ingest(topic, data, source="api", dedupe_key=dedupe)
        client = request.client.host if request.client else "local"
        kernel.db.audit("event.ingest", actor=f"api:{client}",
                        detail={"topic": topic, "event": result.get("event_id"),
                                "matched": result.get("matched"),
                                "duplicate": bool(result.get("duplicate"))})
        return result

    # Read-only on purpose: enabling a plugin runs new code, so it is done at
    # the Core's own console (python -m jarvis plugins enable <id>), not over HTTP.
    @app.get("/api/plugins")
    async def plugins() -> dict[str, Any]:
        mgr = kernel.services.get("plugins")
        if mgr is None:
            return {"root": None, "plugins": [], "note": "plugin manager not loaded"}
        return mgr.status()

    # ---------------------------------------------------------------- memory
    @app.get("/api/memory")
    async def memory(q: str = "", limit: int = 25) -> dict[str, Any]:
        if q:
            res = await kernel.memory.search(q, top_k=limit)
            return {"items": res["items"], "evidence": res["evidence"]}
        rows = kernel.db.query(
            "SELECT id,created_at,kind,text,source,confidence,private,tags FROM memory "
            "WHERE deleted=0 ORDER BY created_at DESC LIMIT ?", (min(limit, 200),))
        return {"items": [dict(r) for r in rows], "evidence": {"method": "list"}}

    # ---------------------------------------------------------------- alerts
    @app.get("/api/alerts")
    async def alerts(limit: int = 50, unacknowledged_only: bool = False) -> dict[str, Any]:
        sql = "SELECT * FROM alerts"
        if unacknowledged_only:
            sql += " WHERE acknowledged=0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        rows = kernel.db.query(sql, (min(limit, 500),))
        return {"alerts": [dict(r) for r in rows]}

    @app.post("/api/alerts/{alert_id}/ack")
    async def ack(alert_id: str) -> dict[str, Any]:
        cur = kernel.db.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))
        return {"ok": cur.rowcount > 0}

    # ---------------------------------------------------------------- metrics
    @app.get("/api/metrics")
    async def metrics(name: str = "cpu.percent", limit: int = 120) -> dict[str, Any]:
        rows = kernel.db.query(
            "SELECT ts,name,value,label FROM metrics WHERE name=? ORDER BY id DESC LIMIT ?",
            (name, min(limit, 2000)))
        return {"name": name, "series": [dict(r) for r in reversed(rows)]}

    # ---------------------------------------------------------------- nodes
    @app.get("/api/nodes")
    async def nodes() -> dict[str, Any]:
        svc = kernel.services.get("nodes")
        if svc is None:
            rows = kernel.db.query("SELECT * FROM nodes ORDER BY name")
            return {"nodes": [dict(r) for r in rows],
                    "note": "node service not loaded; showing last known rows"}
        try:
            return {"nodes": await _maybe_await(svc.list_nodes())}
        except Exception as exc:
            return JSONResponse(status_code=200,
                                content={"nodes": [], "error": str(exc)})

    @app.post("/api/nodes/ping")
    async def node_ping(payload: dict[str, Any]) -> dict[str, Any]:
        svc = kernel.services.get("nodes")
        if svc is None:
            raise HTTPException(status_code=503, detail="node service not loaded")
        node_id = (payload or {}).get("node_id")
        if not node_id:
            raise HTTPException(status_code=400, detail="node_id required")
        return await _maybe_await(svc.ping(node_id))

    @app.get("/api/nodes/{node_id}/sysinfo")
    async def node_sysinfo(node_id: str) -> dict[str, Any]:
        """Ask a node for its real host telemetry (the Nodes panel uses this)."""
        svc = kernel.services.get("nodes")
        if svc is None:
            raise HTTPException(status_code=503, detail="node service not loaded")
        try:
            return await _maybe_await(svc.send_task(node_id, "sysinfo", {}))
        except Exception as exc:
            raise HTTPException(status_code=502,
                                detail=f"{type(exc).__name__}: {exc}") from None

    @app.get("/api/hosts/{node_id}/apps")
    async def host_apps(node_id: str) -> dict[str, Any]:
        """Read-only host app catalog for the native UI."""
        svc = kernel.services.get("nodes")
        if svc is None:
            raise HTTPException(status_code=503, detail="node service not loaded")
        try:
            return await _maybe_await(svc.send_task(
                node_id, "tool", {"tool": "apps.list", "args": {}}))
        except Exception as exc:
            raise HTTPException(status_code=502,
                                detail=f"{type(exc).__name__}: {exc}") from None

    # ---------------------------------------------------------------- discovery / monitoring
    @app.get("/api/discovery")
    async def discovery(refresh: bool = False) -> dict[str, Any]:
        svc = kernel.services.get("discovery")
        if svc is None:
            raise HTTPException(status_code=503, detail="discovery not loaded")
        if refresh and hasattr(svc, "scan"):
            return await _maybe_await(svc.scan())
        return {"hosts": kernel.db.query("SELECT * FROM nodes ORDER BY name") and
                [dict(r) for r in kernel.db.query("SELECT * FROM nodes ORDER BY name")]}

    @app.get("/api/monitoring")
    async def monitoring() -> dict[str, Any]:
        svc = kernel.services.get("monitoring")
        if svc is None:
            raise HTTPException(status_code=503, detail="monitoring not loaded")
        if hasattr(svc, "sample_once"):
            return await _maybe_await(svc.sample_once())
        return {}

    # ---------------------------------------------------------------- defcon / security
    @app.post("/api/defcon")
    async def set_defcon(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            level = int((payload or {}).get("level"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="level 1..5 required")
        kernel.policy.set_defcon(level)
        return {"defcon": kernel.policy.defcon,
                "effective_mode": kernel.policy.effective_mode}

    @app.get("/api/audit")
    async def audit(limit: int = 100) -> dict[str, Any]:
        rows = kernel.db.query(
            "SELECT ts,actor,action,target,severity,detail FROM audit "
            "ORDER BY id DESC LIMIT ?", (min(limit, 1000),))
        return {"audit": [dict(r) for r in rows]}

    # Read-only: it verifies the chain against the anchor on disk and never
    # advances the anchor, so a remote client cannot erase evidence of a break.
    @app.get("/api/audit/verify")
    async def audit_verify() -> dict[str, Any]:
        from ..security.audit_chain import verify
        return verify(kernel.db, kernel.config.data_path)

    # ---------------------------------------------------------------- grants
    @app.get("/api/grants")
    async def grants() -> dict[str, Any]:
        return {"grants": kernel.policy.active_grants()}

    @app.post("/api/grants")
    async def add_grant(payload: dict[str, Any]) -> dict[str, Any]:
        scope = (payload or {}).get("scope")
        if not scope:
            raise HTTPException(status_code=400, detail="scope required")
        gid = kernel.policy.add_grant(
            scope, target=(payload or {}).get("target"),
            ttl_s=(payload or {}).get("ttl_s"),
            note=(payload or {}).get("note", "ui"))
        return {"grant_id": gid}

    # ---------------------------------------------------------------- voice
    @app.get("/api/voice/status")
    async def voice_status() -> dict[str, Any]:
        svc = kernel.services.get("voice")
        if svc is None:
            return {"available": False, "reason": "voice service not loaded"}
        return await _maybe_await(svc.status())

    @app.post("/api/voice/say")
    async def voice_say(payload: dict[str, Any]) -> dict[str, Any]:
        svc = kernel.services.get("voice")
        text = (payload or {}).get("text", "")
        if svc is None:
            return {"ok": False, "reason": "voice service not loaded", "text": text}
        return await _maybe_await(svc.say(text))

    @app.post("/api/voice/listen")
    async def voice_listen(payload: dict[str, Any]) -> dict[str, Any]:
        svc = kernel.services.get("voice")
        if svc is None:
            return {"ok": False, "reason": "voice service not loaded"}
        seconds = float((payload or {}).get("seconds", 6.0))
        return await _maybe_await(svc.listen_once(seconds))

    # ---------------------------------------------------------------- night
    @app.post("/api/night/run")
    async def night_run(payload: dict[str, Any]) -> dict[str, Any]:
        svc = kernel.services.get("night")
        if svc is None:
            raise HTTPException(status_code=503, detail="night runner not loaded")
        objectives = (payload or {}).get("objectives")
        run_id = await _maybe_await(svc.run_now(objectives))
        return {"run_id": run_id}

    # ---------------------------------------------------------------- settings
    _EDITABLE = {
        "policy.mode": lambda v: setattr(kernel.config.policy, "mode", str(v)),
        "persona.address_as": lambda v: setattr(kernel.config.persona, "address_as", str(v)),
        "persona.formality": lambda v: setattr(kernel.config.persona, "formality", float(v)),
        "persona.humor": lambda v: setattr(kernel.config.persona, "humor", float(v)),
        "persona.verbosity": lambda v: setattr(kernel.config.persona, "verbosity", float(v)),
        "voice.enabled": lambda v: setattr(kernel.config.voice, "enabled", bool(v)),
        "voice.tts_enabled": lambda v: setattr(kernel.config.voice, "tts_enabled", bool(v)),
        "voice.stt_enabled": lambda v: setattr(kernel.config.voice, "stt_enabled", bool(v)),
        "voice.wake_enabled": lambda v: setattr(kernel.config.voice, "wake_enabled", bool(v)),
        "voice.wake_model": lambda v: setattr(kernel.config.voice, "wake_model", str(v)),
        "voice.wake_threshold": lambda v: setattr(
            kernel.config.voice, "wake_threshold", _clamp01(v)),
        "voice.default_hosts": lambda v: setattr(
            kernel.config.voice, "default_hosts", _as_list(v)),
        "voice.volume": lambda v: setattr(kernel.config.voice, "volume", _clamp01(v)),
        "voice.length_scale": lambda v: setattr(
            kernel.config.voice, "length_scale", max(0.5, min(2.0, float(v)))),
        "llm.model": lambda v: setattr(kernel.config.llm, "model", str(v)),
        "llm.num_predict": lambda v: setattr(kernel.config.llm, "num_predict", int(v)),
    }

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {
            "core_name": kernel.config.core_name,
            "host_role": kernel.config.host_role,
            "policy": {"mode": kernel.config.policy.mode,
                       "auto_allow": kernel.config.policy.auto_allow,
                       "always_deny": kernel.config.policy.always_deny},
            "persona": {
                "name": kernel.config.persona.name,
                "address_as": kernel.config.persona.address_as,
                "formality": kernel.config.persona.formality,
                "humor": kernel.config.persona.humor,
                "verbosity": kernel.config.persona.verbosity,
                "british": kernel.config.persona.british,
            },
            "voice": {
                "enabled": kernel.config.voice.enabled,
                "stt_enabled": kernel.config.voice.stt_enabled,
                "tts_enabled": kernel.config.voice.tts_enabled,
                "tts_engine": kernel.config.voice.tts_engine,
                "piper_voice": kernel.config.voice.piper_voice,
                "stt_model": kernel.config.voice.stt_model,
                "wake_enabled": kernel.config.voice.wake_enabled,
                "wake_model": kernel.config.voice.wake_model,
                "wake_threshold": kernel.config.voice.wake_threshold,
                "default_hosts": kernel.config.voice.default_hosts,
            },
            "llm": {"provider": kernel.config.llm.provider,
                    "model": kernel.config.llm.model,
                    "base_url": kernel.config.llm.base_url},
            "defcon": kernel.policy.defcon,
            "editable_keys": sorted(_EDITABLE),
        }

    @app.post("/api/settings")
    async def set_settings(payload: dict[str, Any]) -> dict[str, Any]:
        applied, rejected = {}, {}
        for key, value in (payload or {}).items():
            fn = _EDITABLE.get(key)
            if fn is None:
                rejected[key] = "not editable"
                continue
            try:
                fn(value)
                applied[key] = value
            except Exception as exc:
                rejected[key] = str(exc)
        if applied:
            kernel.db.audit("settings.change", actor="owner", detail=redact(applied))
            kernel.bus.publish("settings", changed=applied)
        return {"applied": applied, "rejected": rejected}

    # ---------------------------------------------------------------- diagnostics
    @app.get("/api/diagnostics")
    async def diagnostics() -> dict[str, Any]:
        llm = await kernel.router.health()
        cap: dict[str, Any] = {"llm": llm}
        for name in ("voice", "monitoring", "discovery", "nodes", "night", "security",
                     "sandbox", "runtime", "ops"):
            svc = kernel.services.get(name)
            if svc is None:
                cap[name] = {"loaded": False, "reason": "not built"}
                continue
            entry: dict[str, Any] = {"loaded": True}
            if hasattr(svc, "status"):
                try:
                    entry.update(await _maybe_await(svc.status()) if asyncio.iscoroutinefunction(svc.status)
                                 else svc.status())
                except Exception as exc:
                    entry["error"] = str(exc)
            cap[name] = entry
        cap["db_integrity"] = kernel.db.query_one("PRAGMA integrity_check")["integrity_check"]
        cap["tools"] = len(kernel.registry.names())
        return cap

    # ---------------------------------------------------------------- voice routes
    _include_optional_voice_routes(app, kernel)

    # ---------------------------------------------------------------- openai-compatible
    try:
        from .openai_compat import build_openai_router
        app.include_router(build_openai_router(kernel))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("openai-compatible routes unavailable: %s", exc)

    # ---------------------------------------------------------------- static HUD
    if UI_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")

        @app.get("/")
        async def index() -> Any:
            idx = UI_DIR / "index.html"
            if idx.is_file():
                return FileResponse(str(idx))
            return JSONResponse(status_code=200, content={
                "message": "JARVIS core is running; HUD not installed at jarvis/ui/index.html",
                "health": "/api/health",
            })
    else:
        @app.get("/")
        async def index_missing() -> Any:
            return JSONResponse(status_code=200, content={
                "message": "JARVIS core is running; no HUD directory found.",
                "expected": str(UI_DIR),
                "health": "/api/health",
            })

    return app


def _install_optional_auth(app: FastAPI, kernel: Any) -> None:
    try:
        from .auth import install_auth
    except ModuleNotFoundError:
        if kernel.config.server.allow_lan:
            # Refuse to serve the LAN without the gate rather than serve it open.
            raise RuntimeError("server.allow_lan is true but jarvis.api.auth is missing")
        log.warning("auth module unavailable; serving loopback only")
        return
    install_auth(app, kernel)


def _include_optional_voice_routes(app: FastAPI, kernel: Any) -> None:
    try:
        from .voice_routes import build_voice_router
    except ModuleNotFoundError as exc:
        log.warning("voice routes unavailable: %s", exc)
        return
    app.include_router(build_voice_router(kernel))


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _clamp01(value: Any) -> float:
    return max(0.0, min(1.0, float(value)))


def _as_list(value: Any) -> list[str]:
    """Coerce a comma/space-separated string (or list) into a list of strings."""
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [p.strip() for p in re.split(r"[, ]+", str(value or "").strip()) if p.strip()]
