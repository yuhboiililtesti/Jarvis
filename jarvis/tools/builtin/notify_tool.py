"""``notify.toast`` - desktop notification (LOCAL_EXEC).

Windows: Windows PowerShell 5.1 + the WinRT ``ToastNotificationManager`` API
(no extra package). The title/body are passed through environment variables
and inserted with ``CreateTextNode``, so caller text can never become script
code or toast XML. The toast is attributed to Windows PowerShell's registered
AppUserModelID, which is what lets an unpackaged process show one.
Linux: ``notify-send`` when present.

There is no independent read-back that a toast was displayed (Focus Assist /
Do Not Disturb can suppress it silently), so ``verify`` returns ``None``.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common, _win

log = get_logger("jarvis.tools.notify")

__all__ = ["NotifyToastTool", "TOAST_SCRIPT", "POWERSHELL_AUMID", "health"]

POWERSHELL_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
MAX_TITLE = 100
MAX_BODY = 500

TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:JARVIS_TOAST_TITLE)) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:JARVIS_TOAST_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:JARVIS_TOAST_APPID).Show($toast)
'shown'
""".strip()


def health() -> dict[str, Any]:
    if _win.IS_WINDOWS:
        if shutil.which("powershell"):
            return _common.health_ok("Windows PowerShell WinRT toast")
        return _common.health_fail("powershell.exe not found on PATH")
    if shutil.which("notify-send"):
        return _common.health_ok("notify-send")
    return _common.health_fail("no notifier: install libnotify-bin (notify-send)")


def _clean(text: Any, cap: int) -> str:
    s = "".join(ch if ch >= " " or ch in "\n\t" else " " for ch in str(text or ""))
    return s.strip()[:cap]


@register(["toast", "notify"])
class NotifyToastTool(BaseTool):
    spec = ToolSpec(
        name="notify.toast",
        summary="Show a desktop notification (Windows toast / notify-send).",
        category="notifications",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(
            ToolParam("title", "string", f"Notification title (max {MAX_TITLE} chars).",
                      required=True),
            ToolParam("message", "string", f"Notification body (max {MAX_BODY} chars)."),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {shown_requested, title}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        title = _clean(kwargs.get("title"), MAX_TITLE)
        body = _clean(kwargs.get("message"), MAX_BODY)
        if not title:
            return ToolResult(ok=False, error="title is required")
        gate = _common.unavailable_result(health())
        if gate:
            return gate
        if _common.dry_run_requested(kwargs):
            return _common.preview(target="desktop notification area", operation="notify.toast",
                                   effect=f"show a notification titled {title!r}",
                                   reversible=True, rollback="dismiss the notification",
                                   title=title, message=body)
        try:
            if _win.IS_WINDOWS:
                argv = _win.ps_argv(TOAST_SCRIPT)
                env = {"JARVIS_TOAST_TITLE": title, "JARVIS_TOAST_BODY": body,
                       "JARVIS_TOAST_APPID": POWERSHELL_AUMID}
            else:
                argv = ["notify-send", "--app-name=JARVIS", "--", title, body]
                env = None
            res = await asyncio.to_thread(_win.run_cmd, argv, timeout=20, env=env)
            if not res["ok"]:
                return ToolResult(ok=False, error=("notification failed: "
                                                   f"{res['error'] or res['stderr'].strip()[-300:]}"),
                                  certainty="settled")
            return ToolResult(ok=True, data={"shown_requested": True, "title": title},
                              evidence={"method": argv[0], "exit_code": res["code"]},
                              certainty="unverified")
        except Exception as exc:
            log.warning("notify.toast failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        return None  # display cannot be observed (Focus Assist may suppress it)
