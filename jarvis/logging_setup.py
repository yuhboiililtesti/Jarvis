"""Structured, durable logging for JARVIS.

Writes human-readable text to stderr plus a rotating JSONL file under
data/logs/. The JSONL stream is the machine-readable audit trail consumed by
the monitoring and evidence views. Secrets are never logged: callers must pass
already-redacted payloads (see jarvis.policy.secrets.redact).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path
from typing import Any

__all__ = ["setup_logging", "get_logger", "redact"]

_REDACT_KEYS = {
    "token", "password", "passwd", "secret", "api_key", "apikey",
    "authorization", "auth", "private_key", "credential", "cookie",
}

# Known credential shapes are scrubbed even when they appear under an
# innocuous key such as ``note`` or ``context``. Key-based redaction alone
# cannot catch a secret pasted into a free-form log message.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*(?:bearer\s+)?[^\s,;\"']{8,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[\"']?[^\s,;\"'}]{8,}"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
               re.DOTALL),
)


def _redact_string(value: str) -> str:
    out = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            out = pattern.sub(lambda m: f"{m.group(1)}=***REDACTED***", out)
        else:
            out = pattern.sub("***REDACTED***", out)
    return out


def redact(value: Any) -> Any:
    """Recursively scrub secret keys and recognisable credential strings.

    This is intentionally a best-effort logging boundary, not a substitute for
    a secret manager. It covers structured keys plus common bearer/API/JWT/
    private-key shapes; unknown custom token formats must still be stored in
    the secret store so value-based scrubbing can catch them.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _REDACT_KEYS:
                out[k] = "***REDACTED***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, str):
        return _redact_string(value)
    return value


class _JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key in ("event", "task_id", "tool", "duration_ms", "ok", "extra"):
            if hasattr(record, key):
                payload[key] = redact(getattr(record, key))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", log_dir: Path | None = None,
                  jsonl_name: str = "jarvis") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S"))
    root.addHandler(stream)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / f"{jsonl_name}.jsonl", maxBytes=8_000_000,
            backupCount=5, encoding="utf-8")
        fh.setFormatter(_JsonlFormatter())
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
