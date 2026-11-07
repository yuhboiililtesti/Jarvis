"""Private-cloud access gate for the JARVIS API (HTTP + WebSocket).

Policy (fail closed at every step):

1. **Local** requests are always allowed. "Local" means ALL of: the TCP peer is a
   loopback address, the request carries no proxy/forwarding header, and the
   ``Host`` header is a loopback name or an IP literal. The last two rules stop a
   reverse proxy on the same box (Caddy) from turning every LAN request into a
   "loopback" one, and stop DNS-rebinding pages from borrowing loopback trust.
2. Anything else is **remote**. Remote is refused with 403 unless
   ``config.server.allow_lan`` is true.
3. A remote client must come from an allowed network: ``server.trusted_networks``
   when set, otherwise private ranges only (RFC 1918, CGNAT/Tailscale 100.64/10,
   IPv6 ULA and link-local). ``trusted_networks`` only narrows WHO may try; it
   never waives the token.
4. A remote client must present a valid credential: ``Authorization: Bearer``,
   ``X-Jarvis-Token``, or the HttpOnly ``SameSite=Strict`` session cookie issued by
   the pairing flow. Tokens are never accepted in URLs.
5. Repeated invalid credentials (or pairing codes) from one IP lock that IP out.
6. A browser ``Origin`` that is neither same-origin nor in ``server.cors_origins``
   is refused (cross-site request / cross-site WebSocket hijacking guard).

Secrets: the API token lives only in ``<data>/api-token`` (0600 on POSIX, owner-only
ACL on Windows, best effort). It is never logged, audited, returned by the API or
placed in a URL. The session cookie holds an HMAC derived from the token, so
rotating the token revokes every paired device at once.

CLI (also callable from ``jarvis.__main__`` via :func:`cli_main`)::

    python -m jarvis.api.auth token [--show]   # ensure token; print path + fingerprint
    python -m jarvis.api.auth rotate           # new token; all devices must re-pair
    python -m jarvis.api.auth pair [--ttl 300] # one-time pairing code for a phone
    python -m jarvis.api.auth status
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..logging_setup import get_logger

log = get_logger("jarvis.api.auth")

__all__ = [
    "install_auth", "AuthGate", "AuthMiddleware", "FailureTracker",
    "ensure_token", "rotate_token", "read_token", "token_path", "token_fingerprint",
    "new_pairing_code", "redeem_pairing_code", "burn_pairing_codes", "pending_pairing_codes",
    "session_cookie_value", "cli_main",
    "COOKIE_NAME", "TOKEN_HEADER",
]

# --------------------------------------------------------------------------- constants
TOKEN_FILENAME = "api-token"
PAIRING_FILENAME = "pairing-codes.json"
COOKIE_NAME = "jarvis_session"
TOKEN_HEADER = "x-jarvis-token"
COOKIE_MAX_AGE_S = 30 * 24 * 3600
MAX_CREDENTIAL_LEN = 512

# Pairing codes: 8 symbols from a 30-symbol unambiguous alphabet (~39 bits),
# single use, short-lived, and burnt wholesale after too many global failures.
PAIR_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
PAIR_CODE_LEN = 8
PAIR_CODE_TTL_S = 300
PAIR_CODE_MAX_OUTSTANDING = 5
PAIR_CODE_MAX_GLOBAL_FAILURES = 10

# Per-IP lockout.
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300.0
LOCKOUT_S = 900.0
AUDIT_MAX_PER_MINUTE = 300

PUBLIC_PATHS = frozenset({"/pair", "/api/auth/pair"})        # network-gated, no token
LOOPBACK_ONLY_PATHS = frozenset({"/api/auth/pair/new", "/api/auth/unlock"})  # never remote

# Paths that govern authority (approvals, posture, settings, grants). Even a
# loopback (local) caller must present a valid credential: another local process
# (browser, downloaded tool, plugin) must not approve actions or change security
# settings merely because it shares the loopback interface.
PROTECTED_PREFIXES = ("/api/approvals", "/api/defcon", "/api/settings", "/api/grants")


def _path_protected(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PROTECTED_PREFIXES)

FORWARDING_HEADERS = frozenset({b"x-forwarded-for", b"forwarded", b"x-real-ip",
                                b"x-forwarded-host"})

DEFAULT_REMOTE_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                           "100.64.0.0/10", "fc00::/7", "fe80::/10")

# Starlette's in-process TestClient reports these placeholder names. A real ASGI
# server reports the socket peer as an IP literal, so these strings cannot arrive
# from the network; with any forwarding header present they are not trusted anyway.
_INPROCESS_PEERS = frozenset({"testclient"})
_LOCAL_HOSTNAMES = frozenset({"localhost", "testserver"})

_file_lock = threading.Lock()


# --------------------------------------------------------------------------- token file
def token_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / TOKEN_FILENAME


def _restrict_permissions(path: Path) -> bool:
    """Owner-only access to a secret file. Best effort; never raises."""
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
            return True
        user = os.environ.get("USERNAME")
        if not user:
            return False
        proc = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            log.debug("icacls could not restrict %s (rc=%s)", path.name, proc.returncode)
        return proc.returncode == 0
    except Exception as exc:  # pragma: no cover - platform dependent
        log.debug("could not restrict permissions on %s: %s", path.name, exc)
        return False


def _write_secret(path: Path, text: str) -> None:
    """Atomically write a secret file created owner-only from the start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        _restrict_permissions(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover
                pass


def read_token(data_dir: str | os.PathLike[str]) -> str | None:
    p = token_path(data_dir)
    try:
        value = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def ensure_token(data_dir: str | os.PathLike[str]) -> str:
    """Return the API token, creating ``<data>/api-token`` if it does not exist."""
    with _file_lock:
        existing = read_token(data_dir)
        if existing:
            return existing
        token = secrets.token_urlsafe(32)
        _write_secret(token_path(data_dir), token + "\n")
        log.info("created API token file %s (fingerprint %s)",
                 token_path(data_dir), token_fingerprint(token))
        return token


def rotate_token(data_dir: str | os.PathLike[str]) -> str:
    """Replace the token. Every cookie and outstanding pairing code becomes invalid."""
    with _file_lock:
        token = secrets.token_urlsafe(32)
        _write_secret(token_path(data_dir), token + "\n")
    burn_pairing_codes(data_dir)
    log.warning("API token rotated (new fingerprint %s); paired devices must re-pair",
                token_fingerprint(token))
    return token


def token_fingerprint(token: str) -> str:
    """Non-reversible short identifier for display; never the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def session_cookie_value(token: str) -> str:
    return hmac.new(token.encode("utf-8"), b"jarvis-session-v1", hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- pairing codes
def _pairing_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / PAIRING_FILENAME


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


def _hash_code(code: str) -> str:
    return hashlib.sha256(_normalize_code(code).encode("ascii", "ignore")).hexdigest()


def _load_codes(data_dir: str | os.PathLike[str], now: float) -> list[dict[str, Any]]:
    try:
        raw = json.loads(_pairing_path(data_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict) and isinstance(e.get("h"), str) and \
                isinstance(e.get("exp"), (int, float)) and e["exp"] > now:
            out.append({"h": e["h"], "exp": float(e["exp"])})
    return out


def _save_codes(data_dir: str | os.PathLike[str], entries: list[dict[str, Any]]) -> None:
    _write_secret(_pairing_path(data_dir), json.dumps(entries))


def new_pairing_code(data_dir: str | os.PathLike[str], ttl_s: float = PAIR_CODE_TTL_S,
                     *, now: float | None = None) -> str:
    """Create a single-use pairing code (returned formatted as ``ABCD-EFGH``).

    Only a SHA-256 of the code is stored; the plaintext exists solely in the
    return value, for display on the Core.
    """
    now = time.time() if now is None else now
    ttl_s = max(30.0, min(float(ttl_s), 3600.0))
    code = "".join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(PAIR_CODE_LEN))
    with _file_lock:
        entries = _load_codes(data_dir, now)
        entries.append({"h": _hash_code(code), "exp": now + ttl_s})
        _save_codes(data_dir, entries[-PAIR_CODE_MAX_OUTSTANDING:])
    return f"{code[:4]}-{code[4:]}"


def redeem_pairing_code(data_dir: str | os.PathLike[str], code: str,
                        *, now: float | None = None) -> bool:
    """Consume a code. True exactly once per valid, unexpired code."""
    now = time.time() if now is None else now
    norm = _normalize_code(code or "")
    if len(norm) != PAIR_CODE_LEN:
        return False
    digest = _hash_code(norm).encode("ascii")
    with _file_lock:
        entries = _load_codes(data_dir, now)
        match = None
        for e in entries:   # compare every entry: constant work regardless of position
            if hmac.compare_digest(e["h"].encode("ascii"), digest):
                match = e
        if match is None:
            return False
        entries.remove(match)
        _save_codes(data_dir, entries)
        return True


def burn_pairing_codes(data_dir: str | os.PathLike[str]) -> int:
    with _file_lock:
        n = len(_load_codes(data_dir, time.time()))
        p = _pairing_path(data_dir)
        if p.exists():
            _save_codes(data_dir, [])
        return n


def pending_pairing_codes(data_dir: str | os.PathLike[str]) -> int:
    return len(_load_codes(data_dir, time.time()))


# --------------------------------------------------------------------------- rate limiting
class FailureTracker:
    """Per-key failure counter with a sliding window and a lockout period."""

    def __init__(self, *, max_failures: int = MAX_FAILURES, window_s: float = FAILURE_WINDOW_S,
                 lockout_s: float = LOCKOUT_S, max_entries: int = 4096,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.lockout_s = lockout_s
        self.max_entries = max_entries
        self.clock = clock
        self._fails: OrderedDict[str, list[float]] = OrderedDict()
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def locked_remaining(self, key: str) -> float:
        with self._lock:
            until = self._locked.get(key)
            if until is None:
                return 0.0
            remaining = until - self.clock()
            if remaining <= 0:
                del self._locked[key]
                self._fails.pop(key, None)
                return 0.0
            return remaining

    def record_failure(self, key: str) -> bool:
        """Record one failure; True when this failure starts a lockout."""
        now = self.clock()
        with self._lock:
            hist = [t for t in self._fails.pop(key, []) if now - t < self.window_s]
            hist.append(now)
            self._fails[key] = hist
            while len(self._fails) > self.max_entries:   # bound memory under a flood
                self._fails.popitem(last=False)
            if len(hist) >= self.max_failures:
                self._locked[key] = now + self.lockout_s
                return True
            return False

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


# --------------------------------------------------------------------------- helpers
def _ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    try:
        addr = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_loopback_peer(peer: str | None) -> bool:
    if peer in _INPROCESS_PEERS:
        return True
    addr = _ip(peer)
    return bool(addr and addr.is_loopback)


def _host_only(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    if h.count(":") == 1:
        return h.rsplit(":", 1)[0]
    return h


def _host_is_local(host_header: str | None) -> bool:
    """True for loopback names and IP literals (which DNS rebinding cannot forge)."""
    if not host_header:
        return True     # HTTP/1.0 without Host: not a browser rebinding vector
    h = _host_only(host_header)
    if h in _LOCAL_HOSTNAMES:
        return True
    return _ip(h) is not None


def _headers(scope: dict[str, Any]) -> list[tuple[bytes, bytes]]:
    return list(scope.get("headers") or [])


def _header(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str | None:
    for k, v in headers:
        if k == name:
            return v.decode("latin-1")
    return None


def _parse_networks(entries: Iterable[str]) -> list[Any]:
    nets = []
    for e in entries:
        try:
            nets.append(ipaddress.ip_network(str(e).strip(), strict=False))
        except ValueError:
            log.error("ignoring invalid server.trusted_networks entry %r", e)
    return nets


def _extract_credential(headers: list[tuple[bytes, bytes]]) -> tuple[str | None, str | None]:
    auth = _header(headers, b"authorization")
    if auth:
        scheme, _, value = auth.strip().partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip(), "bearer"
    tok = _header(headers, TOKEN_HEADER.encode())
    if tok and tok.strip():
        return tok.strip(), "header"
    cookie_hdr = _header(headers, b"cookie")
    if cookie_hdr:
        jar = SimpleCookie()
        try:
            jar.load(cookie_hdr)
        except CookieError:
            return None, None
        morsel = jar.get(COOKIE_NAME)
        if morsel is not None and morsel.value:
            return morsel.value, "cookie"
    return None, None


def _same_origin(origin: str, scope: dict[str, Any], host: str | None) -> bool:
    if not host:
        return False
    scheme = scope.get("scheme", "http")
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    return origin.rstrip("/").lower() == f"{scheme}://{host}".lower()


# --------------------------------------------------------------------------- the gate
@dataclass
class Decision:
    allowed: bool
    status: int = 200
    reason: str = "ok"
    via: str | None = None          # loopback | public | bearer | header | cookie
    remote: bool = False
    ip: str | None = None
    retry_after: float | None = None


_WARN_REASONS = frozenset({"invalid_credentials", "locked_out", "lockout_started",
                           "origin_not_allowed", "loopback_only"})


class AuthGate:
    """Decides every request. Holds no plaintext token beyond an mtime-keyed cache."""

    def __init__(self, kernel: Any, data_dir: str | os.PathLike[str] | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 wallclock: Callable[[], float] = time.time) -> None:
        self.kernel = kernel
        self.data_dir = Path(data_dir) if data_dir else Path(kernel.config.data_path)
        self.clock = clock
        self.wallclock = wallclock
        self.failures = FailureTracker(clock=clock)
        self._token_cache: tuple[tuple[int, int, int], str] | None = None
        self._net_cache: tuple[tuple[str, ...], list[Any]] | None = None
        self._audit_minute = -1
        self._audit_count = 0
        self._audit_suppressed = 0
        self._pair_failures = 0
        ensure_token(self.data_dir)

    # -- config (read live so settings changes apply without restart) ------
    @property
    def server_cfg(self) -> Any:
        return self.kernel.config.server

    def allowed_networks(self) -> list[Any]:
        configured = tuple(str(x) for x in (self.server_cfg.trusted_networks or []))
        key = configured or DEFAULT_REMOTE_NETWORKS
        if self._net_cache is None or self._net_cache[0] != key:
            self._net_cache = (key, _parse_networks(key))
        return self._net_cache[1]

    def current_token(self) -> str | None:
        """Re-read the token file when it changes, so `rotate` needs no restart."""
        p = token_path(self.data_dir)
        try:
            st = p.stat()
        except FileNotFoundError:
            self._token_cache = None
            return None
        # os.replace() creates a new file, so the inode/file-id changes even when
        # two writes land inside one mtime tick.
        key = (st.st_mtime_ns, st.st_ino, st.st_size)
        if self._token_cache is None or self._token_cache[0] != key:
            tok = read_token(self.data_dir)
            self._token_cache = (key, tok) if tok else None
        return self._token_cache[1] if self._token_cache else None

    # -- decision ------------------------------------------------------------
    def classify(self, scope: dict[str, Any]) -> Decision:
        client = scope.get("client") or ("", 0)
        peer = str(client[0] or "")
        headers = _headers(scope)
        path = scope.get("path") or "/"
        host = _header(headers, b"host")

        origin = _header(headers, b"origin")
        if origin is not None:
            allowed_origins = {o.rstrip("/").lower() for o in (self.server_cfg.cors_origins or [])}
            if origin.rstrip("/").lower() not in allowed_origins and \
                    not _same_origin(origin, scope, host):
                return Decision(False, 403, "origin_not_allowed", ip=peer)

        proxied = any(k in FORWARDING_HEADERS for k, _ in headers)
        local = _is_loopback_peer(peer) and not proxied and _host_is_local(host)

        if path in LOOPBACK_ONLY_PATHS and not local:
            return Decision(False, 403, "loopback_only", remote=True, ip=peer)
        if local:
            # Authority-governing routes are not free on loopback: require a
            # credential (header, bearer, or session cookie) so a local process
            # cannot approve actions or change posture/settings/grants.
            if _path_protected(path):
                cred, transport = _extract_credential(headers)
                token = self.current_token()
                if not cred:
                    return Decision(False, 401, "missing_credentials", via="loopback", ip=peer)
                if not token:
                    return Decision(False, 401, "no_token_configured", via="loopback", ip=peer)
                if self._credential_ok(cred, transport, token):
                    return Decision(True, via=transport, ip=peer)
                return Decision(False, 401, "invalid_credentials", via="loopback", ip=peer)
            return Decision(True, via="loopback", ip=peer)

        # ---- remote from here on
        if not self.server_cfg.allow_lan:
            return Decision(False, 403, "lan_disabled", remote=True, ip=peer)
        addr = _ip(peer)
        if addr is None:
            return Decision(False, 403, "bad_client_address", remote=True, ip=peer)
        if not any(addr in net for net in self.allowed_networks()
                   if net.version == addr.version):
            return Decision(False, 403, "network_not_allowed", remote=True, ip=peer)
        ip = str(addr)
        remaining = self.failures.locked_remaining(ip)
        if remaining > 0:
            return Decision(False, 429, "locked_out", remote=True, ip=ip,
                            retry_after=remaining)
        if path in PUBLIC_PATHS:
            return Decision(True, via="public", remote=True, ip=ip)

        cred, transport = _extract_credential(headers)
        if not cred:
            return Decision(False, 401, "missing_credentials", remote=True, ip=ip)
        token = self.current_token()
        if not token:
            return Decision(False, 401, "no_token_configured", remote=True, ip=ip)
        if self._credential_ok(cred, transport, token):
            self.failures.clear(ip)
            return Decision(True, via=transport, remote=True, ip=ip)
        if self.failures.record_failure(ip):
            return Decision(False, 429, "lockout_started", remote=True, ip=ip,
                            retry_after=self.failures.lockout_s)
        return Decision(False, 401, "invalid_credentials", remote=True, ip=ip)

    @staticmethod
    def _credential_ok(cred: str, transport: str | None, token: str) -> bool:
        if len(cred) > MAX_CREDENTIAL_LEN:
            return False
        expected = session_cookie_value(token) if transport == "cookie" else token
        return hmac.compare_digest(cred.encode("latin-1", "replace"),
                                   expected.encode("latin-1", "replace"))

    # -- pairing ---------------------------------------------------------------
    def redeem(self, ip: str | None, code: str) -> bool:
        if redeem_pairing_code(self.data_dir, code, now=self.wallclock()):
            if ip:
                self.failures.clear(ip)
            self.audit("auth.paired", ip=ip, detail={"result": "ok"})
            return True
        if ip and not _is_loopback_peer(ip):
            if self.failures.record_failure(ip):
                self.audit("auth.denied", ip=ip, target="/api/auth/pair",
                           detail={"reason": "lockout_started", "status": 429},
                           severity="warn")
        self._pair_failures += 1
        self.audit("auth.pair_failed", ip=ip, severity="warn",
                   detail={"global_failures": self._pair_failures})
        if self._pair_failures >= PAIR_CODE_MAX_GLOBAL_FAILURES:
            burnt = burn_pairing_codes(self.data_dir)
            self._pair_failures = 0
            self.audit("auth.pair_codes_burnt", ip=ip, severity="warn",
                       detail={"burnt": burnt, "reason": "too many failed pairing attempts"})
        return False

    def issue_code(self, ttl_s: float = PAIR_CODE_TTL_S) -> str:
        code = new_pairing_code(self.data_dir, ttl_s, now=self.wallclock())
        self._pair_failures = 0
        self.audit("auth.pair_code_issued", ip="loopback", detail={"ttl_s": ttl_s})
        return code

    def cookie_value(self) -> str | None:
        tok = self.current_token()
        return session_cookie_value(tok) if tok else None

    # -- audit -----------------------------------------------------------------
    def audit(self, action: str, *, ip: str | None, target: str | None = None,
              detail: dict[str, Any] | None = None, severity: str = "info") -> None:
        db = getattr(self.kernel, "db", None)
        if db is None:
            return
        try:
            db.audit(action, actor=f"remote:{ip}" if ip else "remote", target=target,
                     detail=detail, severity=severity)
        except Exception as exc:  # never let audit failure open or crash the gate
            log.warning("auth audit write failed: %s", exc)

    def audit_denial(self, scope: dict[str, Any], d: Decision) -> None:
        """Audit every denial. Under a flood (more than AUDIT_MAX_PER_MINUTE denials in
        one minute) further rows are replaced by one suppression marker and a count,
        so an attacker cannot grow the database without bound."""
        minute = int(self.clock() // 60)
        if minute != self._audit_minute:
            if self._audit_suppressed:
                self.audit("auth.denied_suppressed", ip=None, severity="warn",
                           detail={"suppressed": self._audit_suppressed,
                                   "reason": "audit flood cap"})
            self._audit_minute, self._audit_count, self._audit_suppressed = minute, 0, 0
        self._audit_count += 1
        if self._audit_count > AUDIT_MAX_PER_MINUTE:
            if self._audit_suppressed == 0:
                self.audit("auth.denied_flood", ip=d.ip, severity="warn",
                           detail={"cap_per_minute": AUDIT_MAX_PER_MINUTE})
            self._audit_suppressed += 1
            return
        self.audit("auth.denied", ip=d.ip, target=scope.get("path"),
                   severity="warn" if d.reason in _WARN_REASONS else "info",
                   detail={"reason": d.reason, "status": d.status,
                           "kind": scope.get("type"), "method": scope.get("method")})
        log.info("auth denied %s %s from %s: %s", scope.get("type"), scope.get("path"),
                 d.ip, d.reason)


# --------------------------------------------------------------------------- ASGI middleware
_MESSAGES = {
    "origin_not_allowed": "cross-origin request refused",
    "loopback_only": "this endpoint is only available on the Core itself",
    "lan_disabled": "remote access is disabled (server.allow_lan = false)",
    "bad_client_address": "unrecognised client address",
    "network_not_allowed": "client network is not allowed",
    "locked_out": "too many failed attempts; try again later",
    "lockout_started": "too many failed attempts; try again later",
    "missing_credentials": "authentication required",
    "no_token_configured": "authentication unavailable: no API token on the Core",
    "invalid_credentials": "invalid credentials",
    "gate_error": "access check failed; request denied",
}


class AuthMiddleware:
    """Pure ASGI middleware so the same gate covers HTTP, static files and WebSockets."""

    def __init__(self, app: Any, gate: AuthGate) -> None:
        self.app = app
        self.gate = gate

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        try:
            d = self.gate.classify(scope)
        except Exception as exc:   # fail closed: a gate bug denies, never allows
            log.error("auth gate error (%s); denying request", type(exc).__name__)
            d = Decision(False, 403, "gate_error")
        if d.allowed:
            scope.setdefault("state", {})["jarvis_auth"] = {
                "via": d.via, "remote": d.remote, "ip": d.ip}
            await self.app(scope, receive, send)
            return
        self.gate.audit_denial(scope, d)
        if scope["type"] == "websocket":
            msg = await receive()               # websocket.connect
            if msg.get("type") == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008})
            return
        await _send_denial(scope, send, d)


async def _send_denial(scope: dict[str, Any], send: Any, d: Decision) -> None:
    message = _MESSAGES.get(d.reason, "forbidden")
    headers: list[tuple[bytes, bytes]] = [(b"cache-control", b"no-store")]
    accept = (_header(_headers(scope), b"accept") or "").lower()
    path = scope.get("path") or "/"
    if d.status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="jarvis"'))
    if d.retry_after:
        headers.append((b"retry-after", str(int(d.retry_after) + 1).encode()))
    if d.status == 401 and "text/html" in accept and not path.startswith("/api/"):
        body = (b"<!doctype html><meta name=viewport content='width=device-width'>"
                b"<title>JARVIS</title><p>Authentication required. "
                b"<a href='/pair'>Pair this device</a></p>")
        headers.append((b"content-type", b"text/html; charset=utf-8"))
    else:
        body = json.dumps({"detail": message, "reason": d.reason}).encode()
        headers.append((b"content-type", b"application/json"))
    headers.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": d.status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------- routes
_PAIR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pair with JARVIS</title>
<style>
 body{{font-family:system-ui,sans-serif;background:#0b1118;color:#cfe8ff;margin:0;
      display:flex;min-height:100vh;align-items:center;justify-content:center}}
 form{{background:#111b26;padding:24px;border-radius:12px;max-width:340px;width:calc(100% - 32px)}}
 input{{font-size:1.6rem;letter-spacing:.2em;text-transform:uppercase;width:100%;
       box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #3a5a7a;
       background:#0b1118;color:#fff}}
 button{{margin-top:12px;width:100%;padding:12px;font-size:1rem;border:0;border-radius:8px;
        background:#2f8fdd;color:#fff}}
 p{{font-size:.9rem;opacity:.85}} .err{{color:#ff9a9a}}
</style></head><body>
<form method="post" action="/api/auth/pair">
 <h1>Pair this device</h1>
 <p>On the Core run <code>python -m jarvis.api.auth pair</code> and type the code shown.</p>
 {error}
 <input name="code" autocomplete="one-time-code" autocapitalize="characters"
        placeholder="ABCD-EFGH" maxlength="12" required autofocus>
 <button type="submit">Pair</button>
</form></body></html>"""


def _build_router(gate: AuthGate) -> APIRouter:
    router = APIRouter()

    def _client_ip(request: Request) -> str | None:
        info = request.scope.get("state", {}).get("jarvis_auth") or {}
        return info.get("ip") or (request.client.host if request.client else None)

    def _set_cookie(resp: Response, request: Request) -> None:
        value = gate.cookie_value()
        if value is None:
            return
        resp.set_cookie(COOKIE_NAME, value, max_age=COOKIE_MAX_AGE_S, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/")

    @router.get("/pair", include_in_schema=False)
    async def pair_page(error: int = 0) -> HTMLResponse:
        err = "<p class='err'>That code is invalid or expired.</p>" if error else ""
        return HTMLResponse(_PAIR_PAGE.format(error=err),
                            headers={"cache-control": "no-store"})

    @router.post("/api/auth/pair")
    async def pair(request: Request) -> Response:
        raw = (await request.body())[:2048]
        ctype = request.headers.get("content-type", "")
        is_form = "application/x-www-form-urlencoded" in ctype
        code = ""
        try:
            if is_form:
                code = (urllib.parse.parse_qs(raw.decode("utf-8", "replace")).get("code")
                        or [""])[0]
            else:
                body = json.loads(raw or b"{}")
                code = str(body.get("code", "")) if isinstance(body, dict) else ""
        except (ValueError, UnicodeDecodeError):
            code = ""
        if not gate.redeem(_client_ip(request), code):
            if is_form:
                return RedirectResponse("/pair?error=1", status_code=303)
            return JSONResponse({"ok": False, "error": "invalid or expired pairing code"},
                                status_code=401, headers={"cache-control": "no-store"})
        resp: Response = (RedirectResponse("/", status_code=303) if is_form
                          else JSONResponse({"ok": True, "paired": True}))
        resp.headers["cache-control"] = "no-store"
        _set_cookie(resp, request)
        return resp

    @router.post("/api/auth/pair/new")
    async def pair_new(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        ttl = float((payload or {}).get("ttl_s") or PAIR_CODE_TTL_S)
        return {"code": gate.issue_code(ttl), "expires_in_s": ttl,
                "pair_url_path": "/pair"}

    @router.post("/api/auth/unlock")
    async def unlock(request: Request) -> Response:
        """Set the owner session cookie from the API token (loopback only).

        The HUD / owner tooling calls this once with the value of data/api-token
        so authority-governing routes (approvals, DEFCON, settings, grants) accept
        their requests without handing the token to every local process.
        """
        raw = (await request.body())[:512]
        supplied = ""
        try:
            body = json.loads(raw or b"{}")
            if isinstance(body, dict):
                supplied = str(body.get("token", ""))
        except (ValueError, UnicodeDecodeError):
            supplied = ""
        token = gate.current_token()
        ok = bool(token and supplied) and hmac.compare_digest(
            supplied.encode("latin-1", "replace"), token.encode("latin-1", "replace"))
        if not ok:
            gate.audit("auth.unlock_denied", ip="loopback", severity="warn",
                       detail={"reason": "bad_token"})
            return JSONResponse({"ok": False, "error": "invalid token"},
                                status_code=401, headers={"cache-control": "no-store"})
        resp: Response = JSONResponse({"ok": True, "unlocked": True})
        resp.headers["cache-control"] = "no-store"
        _set_cookie(resp, request)
        gate.audit("auth.unlocked", ip="loopback")
        return resp

    @router.get("/api/auth/status")
    async def status(request: Request) -> dict[str, Any]:
        info = request.scope.get("state", {}).get("jarvis_auth") or {}
        out: dict[str, Any] = {
            "authenticated": True,
            "via": info.get("via"),
            "remote": bool(info.get("remote")),
            "allow_lan": bool(gate.server_cfg.allow_lan),
        }
        if not info.get("remote"):
            out["token_present"] = gate.current_token() is not None
            out["pending_pairing_codes"] = pending_pairing_codes(gate.data_dir)
            out["networks"] = [str(n) for n in gate.allowed_networks()]
        return out

    @router.post("/api/auth/logout")
    async def logout() -> Response:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
        return resp

    return router


def install_auth(app: Any, kernel: Any, *, data_dir: str | os.PathLike[str] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wallclock: Callable[[], float] = time.time) -> AuthGate:
    """Install the gate as ASGI middleware plus the pairing routes.

    Call it AFTER ``app.add_middleware(CORSMiddleware, ...)`` so the gate is the
    outermost layer and sees every request first (including CORS preflights).
    """
    gate = AuthGate(kernel, data_dir, clock=clock, wallclock=wallclock)
    app.state.auth_gate = gate
    app.add_middleware(AuthMiddleware, gate=gate)
    app.include_router(_build_router(gate))
    log.info("auth gate installed (allow_lan=%s, networks=%s)",
             bool(kernel.config.server.allow_lan),
             ",".join(str(n) for n in gate.allowed_networks()))
    return gate


# --------------------------------------------------------------------------- CLI
def cli_main(argv: list[str] | None = None) -> int:
    from ..config import load_config

    parser = argparse.ArgumentParser(prog="python -m jarvis.api.auth",
                                     description="JARVIS private-cloud token and pairing")
    parser.add_argument("--config", default=None)
    parser.add_argument("--data-dir", default=None, help="override the data directory")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("token", help="ensure the API token exists; print path + fingerprint")
    p.add_argument("--show", action="store_true",
                   help="print the token itself to this terminal (do not paste it into chat/logs)")
    sub.add_parser("rotate", help="replace the token; every paired device must re-pair")
    p = sub.add_parser("pair", help="issue a one-time pairing code for a phone/laptop")
    p.add_argument("--ttl", type=float, default=PAIR_CODE_TTL_S, help="seconds (30..3600)")
    sub.add_parser("status", help="show gate configuration (no secrets)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    data_dir = Path(args.data_dir) if args.data_dir else cfg.data_path
    if args.cmd == "token":
        tok = ensure_token(data_dir)
        print(f"token file : {token_path(data_dir)}")
        print(f"fingerprint: {token_fingerprint(tok)}")
        if args.show:
            print(f"token      : {tok}")
            print("(shown once on request; keep it out of chat, screenshots and logs)",
                  file=sys.stderr)
        return 0
    if args.cmd == "rotate":
        tok = rotate_token(data_dir)
        print(f"rotated. new fingerprint: {token_fingerprint(tok)}")
        print("All paired devices and outstanding pairing codes are now invalid.")
        print("A running Core picks up the new token automatically.")
        return 0
    if args.cmd == "pair":
        ensure_token(data_dir)
        code = new_pairing_code(data_dir, args.ttl)
        host = cfg.server.host if cfg.server.host not in ("0.0.0.0", "::") else "<core-ip>"
        print(f"pairing code: {code}   (single use, expires in {int(args.ttl)} s)")
        print(f"on the phone open  http://{host}:{cfg.server.port}/pair  (or your https name)")
        return 0
    if args.cmd == "status":
        tok = read_token(data_dir)
        nets = cfg.server.trusted_networks or list(DEFAULT_REMOTE_NETWORKS)
        print(f"allow_lan        : {cfg.server.allow_lan}")
        print(f"bind host:port   : {cfg.server.host}:{cfg.server.port}")
        print(f"remote networks  : {', '.join(nets)}"
              f"{'' if cfg.server.trusted_networks else '  (default private ranges)'}")
        print(f"token            : {'present, fp ' + token_fingerprint(tok) if tok else 'MISSING'}")
        print(f"pending codes    : {pending_pairing_codes(data_dir)}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
