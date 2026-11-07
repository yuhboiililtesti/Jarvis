"""Email: ``email.draft`` (LOCAL_WRITE) and ``email.send`` (REMOTE, approval-gated).

Drafting and sending are separate tools on purpose: a draft never touches the
network, and ``send`` is the only path that does.

``email.draft`` writes an RFC 5322 ``.eml`` into ``<data>/artifacts/drafts``
(temp file + no-clobber rename) and verifies by re-parsing the file.

``email.send`` uses SMTP from ``config.integrations`` (``smtp_host``,
``smtp_port``, ``smtp_user``) and the secret ``smtp`` (password). It refuses
when any of these is missing. Transport security is mandatory: port 465 uses
implicit TLS, any other port must offer STARTTLS or the send is aborted before
authenticating. One attempt only; failures are reported, never retried. It can
send either fresh fields or an existing draft from the drafts folder.

SMTP gives no independent read-back of delivery, so ``verify`` returns
``None``; the server's per-recipient acceptance is recorded as evidence.
Header values are checked for CR/LF (header injection) and addresses are
validated; the password never appears in results or logs.
"""

from __future__ import annotations

import asyncio
import email
import re
import smtplib
import ssl
import time
import uuid
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common
from .file_ops import _place_no_clobber, _remove_quiet, _tmp_sibling
from .files import _sha256_file

log = get_logger("jarvis.tools.email")

__all__ = ["EmailDraftTool", "EmailSendTool", "drafts_dir", "build_message", "health",
           "MAX_RECIPIENTS"]

MAX_RECIPIENTS = 20
MAX_BODY = 200_000
_ADDR = re.compile(r"^[^@\s<>\"',;]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
                   r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$")

# Tests replace this with a fake; signature mirrors smtplib's constructors.
_SMTP_FACTORY = None


def health() -> dict[str, Any]:
    return _common.health_ok("stdlib email/smtplib; sending needs integrations.smtp_* + secret")


def drafts_dir(ctx: ToolContext) -> Path:
    base = getattr(getattr(ctx, "config", None), "data_path", None)
    return (Path(base) if base else Path.cwd() / "data") / "artifacts" / "drafts"


def _addresses(raw: Any, field: str, required: bool) -> list[str]:
    if raw in (None, "", []):
        if required:
            raise ValueError(f"{field} is required")
        return []
    items = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else None
    if items is None:
        raise ValueError(f"{field} must be a string or list of strings")
    out = []
    for _name, addr in getaddresses([str(i) for i in items]):
        if not _ADDR.match(addr or ""):
            raise ValueError(f"invalid email address in {field}: {addr!r}")
        out.append(addr)
    if not out and required:
        raise ValueError(f"{field} is required")
    return out


def _header(value: Any, field: str, cap: int = 998) -> str:
    text = str(value or "")
    if "\r" in text or "\n" in text:
        raise ValueError(f"{field} must not contain line breaks")
    return text.strip()[:cap]


def _sender(ctx: ToolContext, kwargs: dict[str, Any]) -> str:
    integ = getattr(getattr(ctx, "config", None), "integrations", None)
    sender = str(getattr(integ, "smtp_user", "") or "").strip()
    return sender if _ADDR.match(sender) else "jarvis@localhost"


def build_message(ctx: ToolContext, kwargs: dict[str, Any]) -> EmailMessage:
    to = _addresses(kwargs.get("to"), "to", True)
    cc = _addresses(kwargs.get("cc"), "cc", False)
    if len(to) + len(cc) > MAX_RECIPIENTS:
        raise ValueError(f"at most {MAX_RECIPIENTS} recipients")
    subject = _header(kwargs.get("subject"), "subject")
    body = str(kwargs.get("body") or "")
    if len(body) > MAX_BODY:
        raise ValueError(f"body exceeds {MAX_BODY} characters")
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = _sender(ctx, kwargs)
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="jarvis.local")
    msg["X-Jarvis-Draft"] = "1"
    msg.set_content(body)
    return msg


def _parse(path: Path) -> EmailMessage:
    with path.open("rb") as fh:
        return email.message_from_binary_file(fh, policy=policy.default)  # type: ignore[return-value]


def _msg_fields(msg: EmailMessage) -> dict[str, Any]:
    body = msg.get_body(preferencelist=("plain",))
    return {"from": str(msg.get("From", "")), "to": str(msg.get("To", "")),
            "cc": str(msg.get("Cc", "") or ""), "subject": str(msg.get("Subject", "")),
            "message_id": str(msg.get("Message-ID", "")),
            "body": _lf(body.get_content()) if body is not None else ""}


def _lf(text: str) -> str:
    """Drafts are stored with RFC 5322 CRLF line endings; compare/re-send with LF."""
    return text.replace("\r\n", "\n")


@register(["draft_email", "mail.draft"])
class EmailDraftTool(BaseTool):
    spec = ToolSpec(
        name="email.draft",
        summary="Write an email draft (.eml) to data/artifacts/drafts. Never sends.",
        category="communication",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("to", "array", "Recipient address(es).", required=True),
            ToolParam("subject", "string", "Subject line.", required=True),
            ToolParam("body", "string", "Plain-text body."),
            ToolParam("cc", "array", "CC address(es)."),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {path, to, subject, message_id, sha256}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            try:
                msg = build_message(ctx, kwargs)
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            folder = drafts_dir(ctx)
            name = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:8]}.eml"
            path = folder / name
            fields = _msg_fields(msg)
            if _common.dry_run_requested(kwargs):
                return _common.preview(target=str(path), operation="email.draft",
                                       effect=f"write a draft to {fields['to']} (nothing is sent)",
                                       reversible=True, rollback=f"files.delete path={path}",
                                       subject=fields["subject"])
            raw = msg.as_bytes()

            def _write() -> tuple[str, int]:
                folder.mkdir(parents=True, exist_ok=True)
                tmp = _tmp_sibling(path)
                try:
                    tmp.write_bytes(raw)
                    _place_no_clobber(tmp, path)
                except BaseException:
                    _remove_quiet(tmp)
                    raise
                return _sha256_file(path)
            digest, size = await asyncio.to_thread(_write)
            return ToolResult(ok=True, data={"path": str(path), "to": fields["to"],
                                             "cc": fields["cc"], "subject": fields["subject"],
                                             "message_id": fields["message_id"],
                                             "sha256": digest, "size": size, "sent": False},
                              certainty="unverified")
        except Exception as exc:
            log.warning("email.draft failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        path = Path(result.data["path"])
        try:
            fields = _msg_fields(_parse(path))
            digest, _ = _sha256_file(path)
        except Exception as exc:
            return {"verified": False, "method": "eml-reparse", "note": str(exc)}
        ok = (digest == result.data["sha256"] and fields["subject"] == result.data["subject"]
              and fields["message_id"] == result.data["message_id"])
        return {"verified": ok, "method": "eml-reparse+sha256", "path": str(path)}


def _smtp_settings(ctx: ToolContext) -> tuple[dict[str, Any] | None, str | None]:
    integ = getattr(getattr(ctx, "config", None), "integrations", None)
    host = str(getattr(integ, "smtp_host", "") or "").strip()
    user = str(getattr(integ, "smtp_user", "") or "").strip()
    try:
        port = int(getattr(integ, "smtp_port", 587) or 587)
    except (TypeError, ValueError):
        port = 587
    lookup = getattr(ctx, "secrets", None)
    password = ""
    if callable(lookup):
        try:
            password = str(lookup("smtp") or "")
        except Exception:
            password = ""
    missing = [n for n, v in (("integrations.smtp_host", host), ("integrations.smtp_user", user),
                              ("secret 'smtp'", password)) if not v]
    if missing:
        return None, ("email sending is not configured: missing " + ", ".join(missing)
                      + " (email.draft works without it)")
    return {"host": host, "port": port, "user": user, "password": password}, None


def _send_blocking(settings: dict[str, Any], msg: EmailMessage, recipients: list[str]
                   ) -> dict[str, Any]:
    factory = _SMTP_FACTORY
    context = ssl.create_default_context()
    if settings["port"] == 465:
        cls = factory or smtplib.SMTP_SSL
        server = cls(settings["host"], settings["port"], timeout=30, context=context)
        tls = "implicit"
    else:
        cls = factory or smtplib.SMTP
        server = cls(settings["host"], settings["port"], timeout=30)
        tls = "starttls"
    try:
        if tls == "starttls":
            server.ehlo()
            if not server.has_extn("starttls"):
                raise PermissionError("refused: SMTP server does not offer STARTTLS; "
                                      "not sending credentials or mail in clear text")
            server.starttls(context=context)
            server.ehlo()
        server.login(settings["user"], settings["password"])
        refused = server.send_message(msg, from_addr=settings["user"], to_addrs=recipients)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return {"refused": {k: str(v) for k, v in (refused or {}).items()}, "tls": tls}


@register(["send_email", "mail.send"])
class EmailSendTool(BaseTool):
    spec = ToolSpec(
        name="email.send",
        summary=("Send an email over SMTP (TLS required) - fresh fields or an existing draft. "
                 "Always needs approval."),
        category="communication",
        side_effect=SideEffect.REMOTE,
        needs_network=True,
        params=(
            ToolParam("draft", "string", "File name (or path) of a draft in data/artifacts/drafts."),
            ToolParam("to", "array", "Recipient address(es) (when not sending a draft)."),
            ToolParam("subject", "string", "Subject (when not sending a draft)."),
            ToolParam("body", "string", "Plain-text body (when not sending a draft)."),
            ToolParam("cc", "array", "CC address(es)."),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {to, cc, subject, message_id, accepted, refused, tls}",
        cheap=False,
    )

    def _load_draft(self, ctx: ToolContext, ref: str) -> EmailMessage:
        folder = drafts_dir(ctx).resolve(strict=False)
        candidate = Path(ref)
        path = (candidate if candidate.is_absolute() else folder / candidate.name)
        path = path.resolve(strict=False)
        if path.parent != folder or path.suffix.lower() != ".eml":
            raise PermissionError("refused: drafts must be .eml files directly inside "
                                  f"{folder}")
        if not path.is_file():
            raise ValueError(f"draft not found: {path.name}")
        parsed = _parse(path)
        fields = _msg_fields(parsed)
        # Rebuild through build_message so a hand-edited draft still passes validation.
        to = [a for _n, a in getaddresses([fields["to"]])]
        cc = [a for _n, a in getaddresses([fields["cc"]])] if fields["cc"] else []
        return build_message(ctx, {"to": to, "cc": cc, "subject": fields["subject"],
                                   "body": fields["body"]})

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        settings, why = _smtp_settings(ctx)
        if settings is None:
            return ToolResult(ok=False, error=why, evidence={"health": {"available": False}})
        try:
            draft = str(kwargs.get("draft") or "").strip()
            try:
                if draft:
                    if any(kwargs.get(k) for k in ("to", "subject", "body", "cc")):
                        return ToolResult(ok=False, error="give either draft or fresh fields")
                    msg = await asyncio.to_thread(self._load_draft, ctx, draft)
                else:
                    if not str(kwargs.get("subject") or "").strip():
                        return ToolResult(ok=False, error="subject is required")
                    msg = build_message(ctx, kwargs)
            except PermissionError as exc:
                return ToolResult(ok=False, error=str(exc), evidence={"refused": True})
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            del msg["X-Jarvis-Draft"]
            if msg.get("From"):
                msg.replace_header("From", settings["user"])
            fields = _msg_fields(msg)
            recipients = [a for _n, a in getaddresses([fields["to"], fields["cc"]]) if a]
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target={"to": fields["to"], "cc": fields["cc"],
                            "server": f"{settings['host']}:{settings['port']}"},
                    operation="email.send",
                    effect=f"send '{fields['subject']}' to {len(recipients)} recipient(s)",
                    reversible=False, rollback="none; a sent email cannot be recalled",
                    body_chars=len(fields["body"]))
            res = await asyncio.to_thread(_send_blocking, settings, msg, recipients)
            accepted = [r for r in recipients if r not in res["refused"]]
            ok = bool(accepted) and not res["refused"]
            return ToolResult(
                ok=ok,
                data={"to": fields["to"], "cc": fields["cc"], "subject": fields["subject"],
                      "message_id": fields["message_id"], "accepted": accepted,
                      "refused": res["refused"], "tls": res["tls"]},
                error=None if ok else f"server refused: {res['refused']}",
                evidence={"method": "smtp", "accepted": len(accepted), "tls": res["tls"]},
                certainty="unverified",
            )
        except PermissionError as exc:
            return ToolResult(ok=False, error=str(exc), evidence={"refused": True})
        except smtplib.SMTPAuthenticationError:
            return ToolResult(ok=False, error="SMTP authentication failed (check smtp_user and "
                                              "the 'smtp' secret)", certainty="settled")
        except (smtplib.SMTPException, OSError) as exc:
            return ToolResult(ok=False, error=f"SMTP send failed (not retried): "
                                              f"{type(exc).__name__}: {exc}", certainty="unknown")
        except Exception as exc:
            log.warning("email.send failed: %s", type(exc).__name__)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        return None  # SMTP acceptance is not delivery; no independent read-back exists
