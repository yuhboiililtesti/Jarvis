"""Audio and media-key tools.

``media.volume_get`` / ``media.volume_set`` / ``media.mute`` control the
default playback device; ``media.play_pause`` / ``media.next`` /
``media.previous`` send the virtual media keys that the focused media session
(Spotify, browser, ...) listens to.

Windows: the Core Audio ``IAudioEndpointVolume`` interface is called directly
through ``ctypes`` COM vtables, so no extra package (pycaw/comtypes) is needed.
Media keys are VK_MEDIA_* events via SendInput.
Linux: ``pactl`` (PulseAudio / PipeWire-pulse) for volume and ``playerctl`` for
transport, when present; otherwise an honest "not available" error.

Read-backs: volume_set and mute re-read the endpoint and compare. Media keys
have no reliable read-back of "the track changed", so their ``verify`` returns
``None`` (unverified).
"""

from __future__ import annotations

import asyncio
import ctypes
import re
import shutil
import uuid
from ctypes import wintypes
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common, _win

log = get_logger("jarvis.tools.media")

__all__ = [
    "MediaVolumeGetTool", "MediaVolumeSetTool", "MediaMuteTool", "MediaPlayPauseTool",
    "MediaNextTool", "MediaPreviousTool", "WinAudioBackend", "PactlBackend", "set_backend",
    "health",
]

# --- Core Audio identifiers ------------------------------------------------
CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
CLSCTX_ALL = 0x17
E_RENDER, E_MULTIMEDIA = 0, 1
# vtable slots (IUnknown occupies 0..2)
_ENUM_GET_DEFAULT = 4
_DEVICE_ACTIVATE = 3
_VOL_SET_SCALAR, _VOL_GET_SCALAR, _VOL_SET_MUTE, _VOL_GET_MUTE = 7, 9, 14, 15
_RELEASE = 2

VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB3
_MEDIA_VK = {"play_pause": VK_MEDIA_PLAY_PAUSE, "next": VK_MEDIA_NEXT_TRACK,
             "previous": VK_MEDIA_PREV_TRACK}
_PLAYERCTL = {"play_pause": "play-pause", "next": "next", "previous": "previous"}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _com_method(ptr: int, index: int, *argtypes):
    """Return a callable for vtable slot ``index`` of COM object ``ptr``."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    fn = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return lambda *args: proto(fn)(ptr, *args)


def _release(ptr: int | None) -> None:
    if ptr:
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
        fn = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[_RELEASE]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(fn)(ptr)


class WinAudioBackend:
    """Default render endpoint via IAudioEndpointVolume (raw COM through ctypes)."""

    platform = "windows"

    def __init__(self) -> None:
        if not _win.IS_WINDOWS:
            raise OSError(_win.UNSUPPORTED)
        self.ole32 = ctypes.WinDLL("ole32")
        self.ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
        self.ole32.CoInitializeEx.restype = ctypes.c_long
        self.ole32.CoUninitialize.argtypes = ()
        self.ole32.CoUninitialize.restype = None
        self.ole32.CoCreateInstance.argtypes = (
            ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
        self.ole32.CoCreateInstance.restype = ctypes.HRESULT

    def _with_volume(self, fn):
        hr = self.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        must_uninit = hr in (0, 1)               # S_OK / S_FALSE (RPC_E_CHANGED_MODE: usable)
        enum = dev = vol = None
        try:
            p = ctypes.c_void_p()
            self.ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_MMDeviceEnumerator)), None,
                                        CLSCTX_ALL, ctypes.byref(_guid(IID_IMMDeviceEnumerator)),
                                        ctypes.byref(p))
            enum = p.value
            d = ctypes.c_void_p()
            _com_method(enum, _ENUM_GET_DEFAULT, ctypes.c_int, ctypes.c_int,
                        ctypes.POINTER(ctypes.c_void_p))(E_RENDER, E_MULTIMEDIA, ctypes.byref(d))
            dev = d.value
            v = ctypes.c_void_p()
            _com_method(dev, _DEVICE_ACTIVATE, ctypes.POINTER(_GUID), wintypes.DWORD,
                        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(_guid(IID_IAudioEndpointVolume)), CLSCTX_ALL, None,
                ctypes.byref(v))
            vol = v.value
            return fn(vol)
        finally:
            for ptr in (vol, dev, enum):
                try:
                    _release(ptr)
                except Exception:  # pragma: no cover - defensive
                    pass
            if must_uninit:
                self.ole32.CoUninitialize()

    def get(self) -> dict[str, Any]:
        def _read(vol):
            level = ctypes.c_float()
            _com_method(vol, _VOL_GET_SCALAR, ctypes.POINTER(ctypes.c_float))(
                ctypes.byref(level))
            muted = wintypes.BOOL()
            _com_method(vol, _VOL_GET_MUTE, ctypes.POINTER(wintypes.BOOL))(ctypes.byref(muted))
            return {"level": int(round(level.value * 100)), "muted": bool(muted.value),
                    "scalar": round(float(level.value), 4)}
        return self._with_volume(_read)

    def set_level(self, level: int) -> None:
        self._with_volume(lambda vol: _com_method(
            vol, _VOL_SET_SCALAR, ctypes.c_float, ctypes.c_void_p)(level / 100.0, None))

    def set_mute(self, muted: bool) -> None:
        self._with_volume(lambda vol: _com_method(
            vol, _VOL_SET_MUTE, wintypes.BOOL, ctypes.c_void_p)(1 if muted else 0, None))

    def media_key(self, action: str) -> int:
        vk = _MEDIA_VK[action]
        events = [_win.key_input(vk=vk, flags=_win.KEYEVENTF_EXTENDEDKEY),
                  _win.key_input(vk=vk, flags=_win.KEYEVENTF_EXTENDEDKEY | _win.KEYEVENTF_KEYUP)]
        sent = _win.send_inputs(events)
        if sent != len(events):
            raise OSError(f"Windows inserted {sent}/{len(events)} media key events")
        return sent


class PactlBackend:
    """Linux: PulseAudio/PipeWire via pactl; transport via playerctl."""

    platform = "linux"

    def __init__(self) -> None:
        if not shutil.which("pactl"):
            raise OSError("volume control needs 'pactl' (pulseaudio-utils / pipewire-pulse); "
                          "it was not found on PATH")

    @staticmethod
    def _run(*args: str) -> str:
        res = _win.run_cmd(["pactl", *args], timeout=5)
        if not res["ok"]:
            raise OSError(res["error"] or res["stderr"].strip() or f"pactl {args[0]} failed")
        return res["stdout"]

    def get(self) -> dict[str, Any]:
        out = self._run("get-sink-volume", "@DEFAULT_SINK@")
        m = re.search(r"(\d+)%", out)
        if not m:
            raise OSError(f"could not parse pactl volume output: {out[:120]!r}")
        mute = self._run("get-sink-mute", "@DEFAULT_SINK@")
        return {"level": int(m.group(1)), "muted": "yes" in mute.lower()}

    def set_level(self, level: int) -> None:
        self._run("set-sink-volume", "@DEFAULT_SINK@", f"{level}%")

    def set_mute(self, muted: bool) -> None:
        self._run("set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0")

    def media_key(self, action: str) -> int:
        if not shutil.which("playerctl"):
            raise OSError("media keys need 'playerctl' on Linux; it was not found on PATH")
        res = _win.run_cmd(["playerctl", _PLAYERCTL[action]], timeout=5)
        if not res["ok"]:
            raise OSError(res["error"] or res["stderr"].strip() or "playerctl failed")
        return 1


_BACKEND: Any = None


def set_backend(backend: Any) -> None:
    global _BACKEND
    _BACKEND = backend


def _backend() -> Any:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = WinAudioBackend() if _win.IS_WINDOWS else PactlBackend()
    return _BACKEND


def health() -> dict[str, Any]:
    if _BACKEND is not None:
        return _common.health_ok("backend injected")
    if _win.IS_WINDOWS:
        return _common.health_ok("Core Audio via ctypes COM; media keys via SendInput")
    if shutil.which("pactl"):
        return _common.health_ok("pactl", playerctl=bool(shutil.which("playerctl")))
    return _common.health_fail("volume control needs 'pactl' (pulseaudio-utils / "
                               "pipewire-pulse) on PATH")


def _gate() -> ToolResult | None:
    return _common.unavailable_result(health())


@register(["volume.get", "volume_get"])
class MediaVolumeGetTool(BaseTool):
    spec = ToolSpec(
        name="media.volume_get",
        summary="Read the default playback device's master volume (0-100) and mute state.",
        category="media",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object: {level, muted}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _gate()
        if gate:
            return gate
        try:
            state = await asyncio.to_thread(lambda: _backend().get())
            return ToolResult(ok=True, data=state, evidence={"method": "endpoint-read"})
        except Exception as exc:
            log.warning("media.volume_get failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["volume.set", "volume_set", "set_volume"])
class MediaVolumeSetTool(BaseTool):
    spec = ToolSpec(
        name="media.volume_set",
        summary="Set the default playback device's master volume (0-100).",
        category="media",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(ToolParam("level", "integer", "Target volume percent, 0-100.", required=True),
                _common.DRY_RUN_PARAM),
        returns="object: {level, previous}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _gate()
        if gate:
            return gate
        try:
            try:
                level = _win.int_arg(kwargs, "level")
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            if level is None or not 0 <= level <= 100:
                return ToolResult(ok=False, error="level must be an integer from 0 to 100")
            backend = _backend()
            before = await asyncio.to_thread(backend.get)
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target="default playback device", operation="media.volume_set",
                    effect=f"master volume {before.get('level')} -> {level}",
                    reversible=True,
                    rollback=f"media.volume_set level={before.get('level')}",
                    current=before)
            await asyncio.to_thread(backend.set_level, level)
            return ToolResult(ok=True, data={"level": level, "previous": before.get("level")},
                              evidence={"method": "endpoint-write"}, certainty="unverified")
        except Exception as exc:
            log.warning("media.volume_set failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unknown")

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        try:
            now = await asyncio.to_thread(lambda: _backend().get())
        except Exception as exc:
            return {"verified": False, "method": "endpoint-readback", "note": str(exc)}
        want = int(result.data["level"])
        return {"verified": abs(int(now["level"]) - want) <= 1, "method": "endpoint-readback",
                "expected": want, "observed": now["level"]}


@register(["mute", "volume.mute"])
class MediaMuteTool(BaseTool):
    spec = ToolSpec(
        name="media.mute",
        summary="Mute or unmute the default playback device (omit state to toggle).",
        category="media",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(ToolParam("state", "boolean", "true = mute, false = unmute, omitted = toggle."),
                _common.DRY_RUN_PARAM),
        returns="object: {muted, previous}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _gate()
        if gate:
            return gate
        try:
            try:
                state = _win.bool_arg(kwargs, "state")
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            backend = _backend()
            before = await asyncio.to_thread(backend.get)
            target = (not before["muted"]) if state is None else state
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target="default playback device", operation="media.mute",
                    effect=f"muted {before['muted']} -> {target}", reversible=True,
                    rollback=f"media.mute state={str(before['muted']).lower()}",
                    current=before)
            await asyncio.to_thread(backend.set_mute, target)
            return ToolResult(ok=True, data={"muted": target, "previous": before["muted"]},
                              evidence={"method": "endpoint-write"}, certainty="unverified")
        except Exception as exc:
            log.warning("media.mute failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unknown")

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        try:
            now = await asyncio.to_thread(lambda: _backend().get())
        except Exception as exc:
            return {"verified": False, "method": "endpoint-readback", "note": str(exc)}
        return {"verified": bool(now["muted"]) == bool(result.data["muted"]),
                "method": "endpoint-readback", "expected": result.data["muted"],
                "observed": now["muted"]}


class _MediaKeyTool(BaseTool):
    action = ""

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _gate()
        if gate:
            return gate
        if _common.dry_run_requested(kwargs):
            return _common.preview(
                target="active media session", operation=self.spec.name,
                effect=f"send the {self.action.replace('_', '/')} media key",
                reversible=True if self.action == "play_pause" else "partially",
                rollback=("send media.play_pause again" if self.action == "play_pause"
                          else "send the opposite track key"))
        try:
            await asyncio.to_thread(lambda: _backend().media_key(self.action))
            return ToolResult(ok=True, data={"action": self.action, "sent": True},
                              evidence={"method": "media-key"}, certainty="unverified")
        except Exception as exc:
            log.warning("%s failed: %s", self.spec.name, exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unknown")

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        return None  # no reliable read-back that the player reacted


def _key_spec(action: str, summary: str) -> ToolSpec:
    return ToolSpec(name=f"media.{action}", summary=summary, category="media",
                    side_effect=SideEffect.LOCAL_EXEC, params=(_common.DRY_RUN_PARAM,),
                    returns="object: {action, sent}")


@register(["play_pause", "media.toggle"])
class MediaPlayPauseTool(_MediaKeyTool):
    action = "play_pause"
    spec = _key_spec("play_pause", "Send the Play/Pause media key to the active media session.")


@register(["next_track", "media.next_track"])
class MediaNextTool(_MediaKeyTool):
    action = "next"
    spec = _key_spec("next", "Send the Next Track media key.")


@register(["previous_track", "media.prev"])
class MediaPreviousTool(_MediaKeyTool):
    action = "previous"
    spec = _key_spec("previous", "Send the Previous Track media key.")
