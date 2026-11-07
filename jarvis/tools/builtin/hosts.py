"""Host-agent proxy tools.

The APOS Core owns policy/approval. These tools only forward an approved call
to a registered host node, which executes an allowlisted local JARVIS tool.
No shell, arbitrary exec, input injection, delete or power tool is exposed here.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register

_HOST = ToolParam("host", "string", "Registered node id, e.g. 'pc'.", required=True)


async def _send(ctx: ToolContext, host: str, kind: str,
                payload: dict[str, Any]) -> ToolResult:
    nodes = ctx.services.get("nodes") if ctx.services else None
    if nodes is None:
        return ToolResult(ok=False, error="node service is not available")
    try:
        resp = await nodes.send_task(host, kind, payload)
    except Exception as exc:
        return ToolResult(ok=False, error=f"host '{host}' call failed: {type(exc).__name__}: {exc}")
    if not isinstance(resp, dict):
        return ToolResult(ok=False, error="host returned a non-object response")
    if not resp.get("ok"):
        return ToolResult(ok=False, data=resp.get("data"),
                          error=str(resp.get("error") or "host refused the task"))
    return ToolResult(ok=True, data=resp.get("data") or resp)


@register()
class HostAppsList(BaseTool):
    spec = ToolSpec(
        name="host.apps.list",
        summary="List installed/known applications on a host agent.",
        category="host",
        side_effect=SideEffect.NONE,
        params=(_HOST,),
        returns="object with host app catalog",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        host = str(kwargs.get("host") or "").strip()
        return await _send(ctx, host, "tool", {"tool": "apps.list", "args": {}})


@register()
class HostAppsLaunch(BaseTool):
    spec = ToolSpec(
        name="host.apps.launch",
        summary="Launch an installed application on a host agent.",
        category="host",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(_HOST, ToolParam("app", "string", "Application name/catalog id.", required=True)),
        returns="object with launch evidence",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        host = str(kwargs.get("host") or "").strip()
        app = str(kwargs.get("app") or "").strip()
        return await _send(ctx, host, "tool", {"tool": "apps.launch", "args": {"app": app}})


@register()
class HostWindowsList(BaseTool):
    spec = ToolSpec(
        name="host.windows.list",
        summary="List visible windows on a host agent.",
        category="host",
        side_effect=SideEffect.NONE,
        params=(_HOST,),
        returns="object with window list",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        host = str(kwargs.get("host") or "").strip()
        return await _send(ctx, host, "tool", {"tool": "window.list", "args": {}})


@register()
class HostWindowsFocus(BaseTool):
    spec = ToolSpec(
        name="host.windows.focus",
        summary="Focus a window by hwnd or title on a host agent.",
        category="host",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(_HOST,
                ToolParam("hwnd", "integer", "Exact window handle from host.windows.list."),
                ToolParam("title", "string", "Unique case-insensitive title substring.")),
        returns="object with focused window evidence",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        host = str(kwargs.get("host") or "").strip()
        args: dict[str, Any] = {}
        if kwargs.get("hwnd") not in (None, ""):
            try:
                args["hwnd"] = int(kwargs["hwnd"])
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="hwnd must be an integer")
        if kwargs.get("title"):
            args["title"] = str(kwargs["title"])
        if not args:
            return ToolResult(ok=False, error="hwnd or title is required")
        return await _send(ctx, host, "tool", {"tool": "window.focus", "args": args})


@register()
class HostScreenCapture(BaseTool):
    spec = ToolSpec(
        name="host.screen.capture",
        summary="Capture the screen on a host agent.",
        category="host",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(_HOST, ToolParam("all_screens", "boolean", "Capture all monitors.", default=False)),
        returns="object with screenshot artefact path and dimensions",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        host = str(kwargs.get("host") or "").strip()
        args = {"all_screens": bool(kwargs.get("all_screens") or False)}
        return await _send(ctx, host, "tool", {"tool": "screen.capture", "args": args})


@register()
class HostSystemInfo(BaseTool):
    spec = ToolSpec(
        name="host.system.info",
        summary="Return system information from a host agent.",
        category="host",
        side_effect=SideEffect.NONE,
        params=(_HOST,),
        returns="object with host system information",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        host = str(kwargs.get("host") or "").strip()
        return await _send(ctx, host, "tool", {"tool": "system.info", "args": {}})


@register()
class HostFilesRead(BaseTool):
    spec = ToolSpec(
        name="host.files.read",
        summary="Read a file from a host agent's allowed roots.",
        category="host",
        side_effect=SideEffect.NONE,
        params=(_HOST, ToolParam("path", "string", "Absolute path.", required=True)),
        returns="object with text/bytes preview",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        host = str(kwargs.get("host") or "").strip()
        path = str(kwargs.get("path") or "").strip()
        return await _send(ctx, host, "tool", {"tool": "files.read", "args": {"path": path}})


@register()
@register()
class HostExec(BaseTool):
    spec = ToolSpec(
        name="host.exec",
        summary="Run a bounded command on a host agent (approval-gated).",
        category="host",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(_HOST,
                ToolParam("command", "string", "Command to run in the host shell.", required=True),
                ToolParam("timeout_s", "number", "Maximum seconds (host capped at 300).", default=30.0)),
        returns="object with returncode, stdout and stderr",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        host = str(kwargs.get("host") or "").strip()
        command = str(kwargs.get("command") or "").strip()
        if not command:
            return ToolResult(ok=False, error="command is required")
        timeout = kwargs.get("timeout_s") or 30.0
        return await _send(ctx, host, "exec", {"command": command,
                                              "timeout_s": timeout})


@register()
class HostVoiceSpeak(BaseTool):
    spec = ToolSpec(
        name="host.voice.speak",
        summary="Speak text through a host agent's local speakers.",
        category="host",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(_HOST, ToolParam("text", "string", "Text to speak.", required=True)),
        returns="object with speech evidence",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        host = str(kwargs.get("host") or "").strip()
        text = str(kwargs.get("text") or "").strip()
        return await _send(ctx, host, "voice.speak", {"text": text})


@register()
class HostPower(BaseTool):
    """Controlled power on a host: lock / sleep / shutdown / restart / cancel.

    DESTRUCTIVE so the Core's policy always asks before a shutdown/restart/sleep.
    The node executes the narrow ``power`` task kind, never an arbitrary shell
    command (the exec denylist still refuses ``shutdown``/``reboot`` there).
    """

    spec = ToolSpec(
        name="host.power",
        summary=("Run a controlled power action (lock/sleep/shutdown/restart/cancel) "
                 "on a host."),
        category="host",
        side_effect=SideEffect.DESTRUCTIVE,
        params=(
            _HOST,
            ToolParam("action", "string", "Power verb.", required=True,
                      enum=("lock", "sleep", "shutdown", "restart", "cancel")),
            ToolParam("delay_s", "integer",
                      "Cancellable delay for shutdown/restart (30-86400 s).", default=60),
            ToolParam("reason", "string", "Short reason shown in the OS warning."),
            ToolParam("dry_run", "boolean", "Preview without acting.", default=False),
        ),
        returns="object with the power action result",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        host = str(kwargs.get("host") or "").strip()
        action = str(kwargs.get("action") or "").strip().lower()
        if not host:
            return ToolResult(ok=False, error="host is required")
        if action not in ("lock", "sleep", "shutdown", "restart", "cancel"):
            return ToolResult(ok=False,
                              error="action must be lock/sleep/shutdown/restart/cancel")
        if bool(kwargs.get("dry_run")):
            return ToolResult(ok=True, data={
                "dry_run": True, "host": host, "action": action,
                "effect": f"run power action '{action}' on host '{host}'"})
        delay_s = kwargs.get("delay_s") or 60
        reason = str(kwargs.get("reason") or "").strip()
        return await _send(ctx, host, "power",
                           {"action": action, "delay_s": delay_s, "reason": reason})


# Host-name aliases so the owner can say "pc", "pc-001", "laptop" or "apos"
# (PC-002) interchangeably. The canonical node ids come from config.nodes.known.
_HOST_ALIASES = {
    "pc": "pc", "pc-001": "pc", "pc001": "pc", "win": "pc", "windows": "pc",
    "laptop": "laptop", "laptop-001": "laptop", "laptop001": "laptop",
    "apos": "apos", "pc-002": "apos", "pc002": "apos", "pc2": "apos",
    "server": "apos", "core": "apos", "local": "apos",
}


@register()
class VoiceSpeak(BaseTool):
    """Synthesise once with the core's Jarvis voice, broadcast to chosen hosts.

    ``hosts`` accepts ``pc`` / ``laptop`` / ``apos`` (or their aliases), a
    comma-separated list, or ``all`` (every known node plus the local core).
    ``apos`` is the local machine; the other names are node ids that receive the
    *same* WAV audio via ``voice.play``, so every speaker gets the identical
    high-quality voice instead of its own local TTS.
    """

    spec = ToolSpec(
        name="voice.speak",
        summary=("Speak text in the Jarvis voice on chosen hosts "
                 "('pc', 'laptop', 'apos', or 'all')."),
        category="voice",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(
            ToolParam("text", "string", "Text to speak.", required=True),
            ToolParam("hosts", "string",
                      "Comma-separated hosts to speak on: pc, laptop, apos, or 'all'. "
                      "Defaults to voice.default_hosts.", default=""),
            ToolParam("voice", "string", "Optional TTS voice override."),
            ToolParam("effect", "string", "Optional effect override ('jarvis' or 'none')."),
        ),
        returns="object listing which hosts spoke and any per-host errors",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        text = str(kwargs.get("text") or "").strip()
        if not text:
            return ToolResult(ok=False, error="text is required")
        voice_svc = (ctx.services or {}).get("voice") if ctx.services else None
        if voice_svc is None or not getattr(voice_svc, "enabled", True):
            return ToolResult(ok=False, error="voice service is not available")
        if not hasattr(voice_svc, "speaker"):
            return ToolResult(ok=False, error="voice service has no synthesiser")

        synth = await voice_svc.speaker.synthesize(
            text, voice=kwargs.get("voice") or None,
            effect=kwargs.get("effect") or None)
        if not (synth.get("ok") and synth.get("path")):
            return ToolResult(ok=False,
                              error=synth.get("reason") or "speech synthesis failed",
                              data={"engine": synth.get("engine")})
        path = synth["path"]
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            return ToolResult(ok=False, error=f"could not read synthesized audio: {exc}")
        audio_b64 = base64.b64encode(raw).decode("ascii")

        hosts = self._resolve_hosts(kwargs.get("hosts"), ctx)
        if not hosts:
            return ToolResult(ok=False, error="no target hosts resolved")

        nodes = (ctx.services or {}).get("nodes") if ctx.services else None
        spoken: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for host in hosts:
            if host == "apos":
                played = await asyncio.to_thread(voice_svc.player.play_file, path)
                if played.get("ok"):
                    spoken["apos"] = {"played": True, "engine": "local"}
                else:
                    errors["apos"] = played.get("reason", "local playback failed")
                continue
            if nodes is None:
                errors[host] = "node service is not available"
                continue
            try:
                resp = await nodes.send_task(
                    host, "voice.play",
                    {"audio_b64": audio_b64,
                     "sample_rate": synth.get("sample_rate"),
                     "duration_s": synth.get("duration_s")})
            except Exception as exc:
                errors[host] = f"{type(exc).__name__}: {exc}"
                continue
            if isinstance(resp, dict) and resp.get("ok"):
                spoken[host] = resp.get("data") or {"played": True}
            else:
                errors[host] = (resp.get("error") if isinstance(resp, dict)
                                else "refused")

        data: dict[str, Any] = {
            "text": text, "engine": synth.get("engine"),
            "sample_rate": synth.get("sample_rate"),
            "duration_s": synth.get("duration_s"),
            "spoken": spoken, "errors": errors,
        }
        if not spoken:
            return ToolResult(ok=False, data=data,
                              error="no host played audio: " + "; ".join(
                                  f"{k}: {v}" for k, v in errors.items()))
        return ToolResult(ok=True, data=data)

    @staticmethod
    def _resolve_hosts(value: Any, ctx: ToolContext) -> list[str]:
        cfg = ctx.config
        known: list[str] = []
        if cfg is not None:
            known = list((getattr(getattr(cfg, "nodes", None), "known", {}) or {}).keys())
        if isinstance(value, (list, tuple)):
            parts = [str(x).strip().lower() for x in value if str(x).strip()]
        else:
            parts = [p.strip().lower() for p in str(value or "").split(",") if p.strip()]
        if not parts:
            default = getattr(getattr(cfg, "voice", None), "default_hosts", []) or []
            parts = [str(p).strip().lower() for p in default if str(p).strip()]
        out: list[str] = []
        for p in parts:
            p = _HOST_ALIASES.get(p, p)
            if p == "all":
                for h in (*known, "apos"):
                    if h not in out:
                        out.append(h)
            elif p not in out:
                out.append(p)
        return out
