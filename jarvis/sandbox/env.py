"""Environment scrubbing for child processes.

JARVIS keeps secrets in its own process environment (``JARVIS_SECRET_<NAME>``)
as well as in ``data/secrets.json``. A child process inherits the entire
environment by default, so a command as ordinary as ``Get-ChildItem env:`` (or
``env`` on POSIX) inside ``shell.run`` would hand a secret straight to whatever
ran the command.

Every process JARVIS starts therefore gets a **scrubbed** environment:

  * variables whose *name* starts with ``JARVIS_SECRET_`` are removed;
  * any variable whose *value* equals a known secret is removed, whatever it is
    called (defence in depth, in case a secret was also exported under another
    name).

Short values (fewer than :data:`MIN_SECRET_LEN` characters) are never matched by
value, because dropping every variable equal to ``"1"`` would silently break the
child for no security gain.

Nothing here ever logs or returns a secret value: values are only ever compared.
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping

__all__ = ["SECRET_PREFIX", "MIN_SECRET_LEN", "sanitized_env", "secret_values_of"]

SECRET_PREFIX = "JARVIS_SECRET_"

# A secret shorter than this is not used for *value* matching (it would match
# unrelated variables); name-prefix removal is unaffected.
MIN_SECRET_LEN = 6


def secret_values_of(secrets: object, minimum: int = MIN_SECRET_LEN) -> set[str]:
    """Collect the known secret *values* from a SecretStore-like object.

    Used only for comparison; the result is never logged, returned to a model or
    written anywhere. A store that cannot enumerate its values yields no matches
    rather than raising.
    """
    out: set[str] = set()
    if secrets is None:
        return out
    names = getattr(secrets, "names", None)
    get = getattr(secrets, "get", None)
    if not callable(names) or not callable(get):
        return out
    try:
        for name in names():
            try:
                value = get(name)
            except Exception:  # pragma: no cover - a broken store must not break a tool
                continue
            if isinstance(value, str) and len(value) >= minimum:
                out.add(value)
    except Exception:  # pragma: no cover - defensive
        return set()
    return out


def sanitized_env(*, base: Mapping[str, str] | None = None,
                  extra: Mapping[str, str] | None = None,
                  secret_values: Iterable[str] = ()) -> dict[str, str]:
    """Return a copy of the environment safe to hand to a child process.

    ``base`` defaults to ``os.environ``. ``extra`` is merged *after* scrubbing
    (a tool's own non-secret inputs, e.g. a toast title); its entries are
    scrubbed by the same rules so a caller cannot re-introduce a secret by name.
    """
    source = os.environ if base is None else base
    match = {v for v in secret_values if isinstance(v, str) and len(v) >= MIN_SECRET_LEN}

    def unused(key: str, value: str) -> bool:
        if key.upper().startswith(SECRET_PREFIX):
            return True
        return bool(value) and value in match

    out: dict[str, str] = {}
    for key, value in source.items():
        if not isinstance(value, str):
            continue
        if unused(key, value):
            continue
        out[key] = value
    if extra:
        for key, value in extra.items():
            text = str(value)
            if unused(str(key), text):
                continue
            out[str(key)] = text
    return out
