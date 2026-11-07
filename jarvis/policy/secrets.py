"""Secret handling.

Secrets never enter model context and are never written to the audit log. They
live in an owner-only file (data/secrets.json, mode 0600 on POSIX) or in
environment variables named JARVIS_SECRET_<NAME>. Tools receive a secret only by
asking the broker for a handle at execution time.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger, redact

log = get_logger("jarvis.secrets")

__all__ = ["SecretStore"]

# Runtime token files are not in secrets.json but are equally sensitive. Load
# them into the same value-based scrubber so a pasted API/node/Anton token is
# redacted at the audit boundary.
TOKEN_FILES = ("api-token", "node-token", "anton-token")
TOKEN_ENV = ("JARVIS_API_TOKEN", "JARVIS_NODE_TOKEN", "JARVIS_ANTON_TOKEN")


class SecretStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._cache: dict[str, str] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path.is_file():
            try:
                self._cache.update(json.loads(self.path.read_text(encoding="utf-8")))
            except Exception as exc:  # pragma: no cover
                log.warning("could not read secrets file: %s", exc)
        # env overrides
        for k, v in os.environ.items():
            if k.startswith("JARVIS_SECRET_"):
                self._cache[k[len("JARVIS_SECRET_"):].lower()] = v
        self._load_tokens()

    def _load_tokens(self) -> None:
        """Load runtime token files/env values for audit redaction.

        Re-read on each scrub so a rotated token is covered without a restart.
        Values are stored under internal names; ``redacted()`` never exposes the
        value, only the key.
        """
        for name in TOKEN_FILES:
            path = self.path.parent / name
            try:
                if path.is_file():
                    value = path.read_text(encoding="utf-8").strip()
                    if len(value) >= 6:
                        self._cache[f"__file_{name}"] = value
            except OSError:
                pass
        for env_name in TOKEN_ENV:
            value = os.environ.get(env_name, "").strip()
            if len(value) >= 6:
                self._cache[f"__env_{env_name.lower()}"] = value

    def get(self, name: str, default: str | None = None) -> str | None:
        self._load()
        return self._cache.get(name.lower(), default)

    def set(self, name: str, value: str) -> None:
        self._load()
        self._cache[name.lower()] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - windows
            pass
        self.db_note(name)

    def db_note(self, name: str) -> None:
        log.info("secret '%s' stored (value not logged)", name)

    def names(self) -> list[str]:
        self._load()
        return sorted(self._cache)

    def scrub(self, text: Any) -> Any:
        """Replace any known secret *value* in a string with a marker.

        Used at the audit boundary so a pasted token or password is not stored
        verbatim. Values shorter than six characters are not matched (the same
        rule as the child-environment scrubber); the result never reveals which
        secret matched.
        """
        if not isinstance(text, str) or not text:
            return text
        self._load()
        self._load_tokens()
        values = sorted(
            {v for v in self._cache.values()
             if isinstance(v, str) and len(v) >= 6},
            key=len, reverse=True)
        for value in values:
            if value in text:
                text = text.replace(value, "***REDACTED***")
        return text

    def redacted(self) -> dict[str, Any]:
        self._load()
        return redact({k: "***" for k in self._cache})
