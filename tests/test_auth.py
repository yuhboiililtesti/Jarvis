"""Private-cloud gate: loopback trust, LAN token auth, pairing, lockout, no leaks.

Every test spoofs the client address with ``TestClient(app, client=(ip, port))``.
No network, no model. The token is read from the tmp data dir and is asserted to
be ABSENT from every response, header, log line, audit row and CLI output.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from jarvis.api import auth  # noqa: E402

LAN_IP = "10.0.0.50"
OTHER_LAN_IP = "10.0.0.51"


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(cfg, db):
    """A tiny app with the real gate installed and a fake kernel (config + db)."""
    clock = FakeClock()
    wall = FakeClock(1_800_000_000.0)
    kernel = SimpleNamespace(config=cfg, db=db)
    app = FastAPI()

    @app.get("/api/ping")
    async def ping() -> dict:
        return {"pong": True}

    @app.post("/api/echo")
    async def echo(payload: dict) -> dict:
        return payload

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        await sock.send_text("hello")
        await sock.close()

    gate = auth.install_auth(app, kernel, clock=clock, wallclock=wall)
    token = auth.read_token(cfg.data_path)
    assert token and len(token) >= 40
    seen: list[str] = []          # every response body + headers, for the leak check

    def client(ip: str = "127.0.0.1", **kw) -> TestClient:
        return TestClient(app, client=(ip, 51000), **kw)

    def record(resp):
        seen.append(resp.text)
        seen.extend(f"{k}: {v}" for k, v in resp.headers.items())
        return resp

    return SimpleNamespace(app=app, gate=gate, token=token, cfg=cfg, db=db, clock=clock,
                           wall=wall, client=client, record=record, seen=seen)


def _audit_reasons(db) -> list[str]:
    import json
    rows = db.query("SELECT detail FROM audit WHERE action='auth.denied' ORDER BY id")
    return [json.loads(r["detail"])["reason"] for r in rows]


# ----------------------------------------------------------------- loopback / LAN switch
@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "127.8.9.10", "::ffff:127.0.0.1",
                                "testclient"])
def test_loopback_always_allowed_without_token(env, ip):
    assert env.cfg.server.allow_lan is False
    r = env.record(env.client(ip).get("/api/ping"))
    assert r.status_code == 200 and r.json() == {"pong": True}


def test_non_loopback_forbidden_when_lan_disabled_even_with_token(env):
    c = env.client(LAN_IP)
    r = env.record(c.get("/api/ping", headers={"Authorization": f"Bearer {env.token}"}))
    assert r.status_code == 403
    assert r.json()["reason"] == "lan_disabled"
    assert "lan_disabled" in _audit_reasons(env.db)


def test_missing_and_wrong_token_are_401(env):
    env.cfg.server.allow_lan = True
    c = env.client(LAN_IP)
    r = env.record(c.get("/api/ping"))
    assert r.status_code == 401 and r.json()["reason"] == "missing_credentials"
    assert r.headers["www-authenticate"].startswith("Bearer")
    r = env.record(c.get("/api/ping", headers={"Authorization": "Bearer wrong-token"}))
    assert r.status_code == 401 and r.json()["reason"] == "invalid_credentials"
    r = env.record(c.get("/api/ping", headers={"X-Jarvis-Token": "nope"}))
    assert r.status_code == 401
    assert "invalid_credentials" in _audit_reasons(env.db)


def test_token_in_url_is_not_accepted(env):
    env.cfg.server.allow_lan = True
    r = env.record(env.client(LAN_IP).get(f"/api/ping?token={env.token}"))
    assert r.status_code == 401


@pytest.mark.parametrize("transport", ["bearer", "header"])
def test_correct_token_accepted_via_header_transports(env, transport):
    env.cfg.server.allow_lan = True
    headers = ({"Authorization": f"Bearer {env.token}"} if transport == "bearer"
               else {"X-Jarvis-Token": env.token})
    r = env.record(env.client(LAN_IP).get("/api/ping", headers=headers))
    assert r.status_code == 200 and r.json() == {"pong": True}


def test_non_ascii_credential_is_rejected_not_crashed(env):
    env.cfg.server.allow_lan = True
    r = env.client(LAN_IP).get("/api/ping", headers={"X-Jarvis-Token": "töken".encode("latin-1")})
    assert r.status_code == 401


# ----------------------------------------------------------------- loopback spoofing guards
def test_forwarded_request_from_loopback_is_treated_as_remote(env):
    # A reverse proxy on the same box must not make LAN clients look local.
    c = env.client("127.0.0.1")
    r = env.record(c.get("/api/ping", headers={"X-Forwarded-For": "10.0.0.77"}))
    assert r.status_code == 403 and r.json()["reason"] == "lan_disabled"


def test_dns_rebinding_host_on_loopback_is_not_trusted(env):
    r = env.record(env.client("127.0.0.1").get("/api/ping",
                                                headers={"Host": "evil.example:8765"}))
    assert r.status_code == 403
    r = env.client("127.0.0.1").get("/api/ping", headers={"Host": "localhost:8765"})
    assert r.status_code == 200


def test_cross_site_origin_refused_same_origin_allowed(env):
    c = env.client("127.0.0.1")
    r = env.record(c.post("/api/echo", json={"a": 1},
                          headers={"Origin": "http://evil.example"}))
    assert r.status_code == 403 and r.json()["reason"] == "origin_not_allowed"
    r = c.post("/api/echo", json={"a": 1}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    r = c.post("/api/echo", json={"a": 1}, headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200      # listed in server.cors_origins


# ----------------------------------------------------------------- trusted networks
def test_trusted_networks_narrow_who_may_try(env):
    env.cfg.server.allow_lan = True
    env.cfg.server.trusted_networks = ["10.0.0.0/24"]
    good = {"Authorization": f"Bearer {env.token}"}
    assert env.client("10.0.0.5").get("/api/ping", headers=good).status_code == 200
    r = env.record(env.client("10.0.1.5").get("/api/ping", headers=good))
    assert r.status_code == 403 and r.json()["reason"] == "network_not_allowed"
    # Being inside a trusted network never waives the token.
    assert env.client("10.0.0.5").get("/api/ping").status_code == 401


def test_default_networks_are_private_ranges_only(env):
    env.cfg.server.allow_lan = True
    good = {"Authorization": f"Bearer {env.token}"}
    assert env.client("192.168.1.20").get("/api/ping", headers=good).status_code == 200
    assert env.client("100.101.102.103").get("/api/ping", headers=good).status_code == 200
    r = env.client("8.8.8.8").get("/api/ping", headers=good)
    assert r.status_code == 403 and r.json()["reason"] == "network_not_allowed"


def test_all_invalid_trusted_networks_fail_closed(env):
    env.cfg.server.allow_lan = True
    env.cfg.server.trusted_networks = ["not-a-cidr"]
    good = {"Authorization": f"Bearer {env.token}"}
    assert env.client(LAN_IP).get("/api/ping", headers=good).status_code == 403


# ----------------------------------------------------------------- websocket
def test_websocket_loopback_connects(env):
    with env.client("127.0.0.1").websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hello"


def test_websocket_remote_gated(env):
    with pytest.raises(WebSocketDisconnect) as exc:       # LAN disabled
        with env.client(LAN_IP).websocket_connect("/ws"):
            pass
    assert exc.value.code == 1008
    env.cfg.server.allow_lan = True
    with pytest.raises(WebSocketDisconnect):              # no token
        with env.client(LAN_IP).websocket_connect("/ws"):
            pass
    with env.client(LAN_IP).websocket_connect(
            "/ws", headers={"Authorization": f"Bearer {env.token}"}) as ws:
        assert ws.receive_text() == "hello"


def test_websocket_cross_site_origin_refused_on_loopback(env):
    with pytest.raises(WebSocketDisconnect):
        with env.client("127.0.0.1").websocket_connect(
                "/ws", headers={"Origin": "http://evil.example"}):
            pass


# ----------------------------------------------------------------- lockout
def test_rate_limit_lockout_and_expiry(env):
    env.cfg.server.allow_lan = True
    c = env.client(LAN_IP)
    bad = {"Authorization": "Bearer wrong"}
    codes = [c.get("/api/ping", headers=bad).status_code for _ in range(auth.MAX_FAILURES)]
    assert codes[:-1] == [401] * (auth.MAX_FAILURES - 1)
    assert codes[-1] == 429
    good = {"Authorization": f"Bearer {env.token}"}
    r = env.record(c.get("/api/ping", headers=good))
    assert r.status_code == 429 and r.json()["reason"] == "locked_out"   # even the right token
    assert int(r.headers["retry-after"]) > 0
    # another LAN host is unaffected
    assert env.client(OTHER_LAN_IP).get("/api/ping", headers=good).status_code == 200
    env.clock.t += auth.LOCKOUT_S + 1
    assert c.get("/api/ping", headers=good).status_code == 200
    assert "lockout_started" in _audit_reasons(env.db)


def test_missing_credentials_do_not_count_toward_lockout(env):
    env.cfg.server.allow_lan = True
    c = env.client(LAN_IP)
    for _ in range(auth.MAX_FAILURES * 3):            # e.g. a phone loading assets unpaired
        assert c.get("/api/ping").status_code == 401
    good = {"Authorization": f"Bearer {env.token}"}
    assert c.get("/api/ping", headers=good).status_code == 200


def test_failures_outside_window_do_not_accumulate(env):
    env.cfg.server.allow_lan = True
    c = env.client(LAN_IP)
    for _ in range(auth.MAX_FAILURES * 2):
        assert c.get("/api/ping", headers={"X-Jarvis-Token": "x"}).status_code == 401
        env.clock.t += auth.FAILURE_WINDOW_S / (auth.MAX_FAILURES - 1) + 1


# ----------------------------------------------------------------- pairing
def test_pairing_code_single_use_sets_strict_httponly_cookie(env):
    env.cfg.server.allow_lan = True
    r = env.client("127.0.0.1").post("/api/auth/pair/new")
    assert r.status_code == 200
    code = r.json()["code"]
    assert len(code) == 9 and code[4] == "-"

    phone = env.client("10.0.0.80")
    assert phone.get("/api/ping").status_code == 401
    r = env.record(phone.post("/api/auth/pair", json={"code": code.lower()}))
    assert r.status_code == 200 and r.json() == {"ok": True, "paired": True}
    set_cookie = r.headers["set-cookie"]
    assert auth.COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie and "SameSite=strict" in set_cookie.replace("Strict", "strict")
    assert env.token not in set_cookie
    # TestClient keeps the cookie -> authenticated by cookie
    r = phone.get("/api/ping")
    assert r.status_code == 200
    # single use
    other = env.client("10.0.0.81")
    r = env.record(other.post("/api/auth/pair", json={"code": code}))
    assert r.status_code == 401


def test_pairing_code_expires(env):
    env.cfg.server.allow_lan = True
    code = env.client("127.0.0.1").post("/api/auth/pair/new", json={"ttl_s": 60}).json()["code"]
    env.wall.t += 61
    r = env.client("10.0.0.80").post("/api/auth/pair", json={"code": code})
    assert r.status_code == 401


def test_pairing_form_flow_redirects_home(env):
    env.cfg.server.allow_lan = True
    code = env.gate.issue_code()
    phone = env.client("10.0.0.82", follow_redirects=False)
    page = phone.get("/pair")
    assert page.status_code == 200 and "<form" in page.text
    r = phone.post("/api/auth/pair", data={"code": code},
                   headers={"Origin": "http://testserver"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert auth.COOKIE_NAME in r.headers["set-cookie"]
    r = phone.post("/api/auth/pair", data={"code": code})
    assert r.status_code == 303 and r.headers["location"].startswith("/pair?error=1")


def test_issue_code_is_loopback_only(env):
    env.cfg.server.allow_lan = True
    r = env.client(LAN_IP).post("/api/auth/pair/new",
                                headers={"Authorization": f"Bearer {env.token}"})
    assert r.status_code == 403 and r.json()["reason"] == "loopback_only"


def test_pairing_page_needs_lan_enabled(env):
    assert env.client(LAN_IP).get("/pair").status_code == 403


def test_bad_pairing_codes_lock_out_ip(env):
    env.cfg.server.allow_lan = True
    phone = env.client(LAN_IP)
    for _ in range(auth.MAX_FAILURES):
        phone.post("/api/auth/pair", json={"code": "AAAA-AAAA"})
    r = phone.post("/api/auth/pair", json={"code": env.gate.issue_code()})
    assert r.status_code == 429


def test_global_pairing_failures_burn_outstanding_codes(env):
    env.cfg.server.allow_lan = True
    code = env.gate.issue_code()
    for i in range(auth.PAIR_CODE_MAX_GLOBAL_FAILURES):     # distributed guessing
        env.client(f"10.0.1.{i + 1}").post("/api/auth/pair", json={"code": "ZZZZ-ZZZZ"})
    r = env.client("10.0.2.1").post("/api/auth/pair", json={"code": code})
    assert r.status_code == 401


def test_pairing_store_holds_only_hashes(env):
    code = env.gate.issue_code()
    raw = (env.cfg.data_path / auth.PAIRING_FILENAME).read_text(encoding="utf-8")
    assert code.replace("-", "") not in raw and code not in raw


# ----------------------------------------------------------------- rotation
def test_rotate_token_revokes_old_token_and_cookies_without_restart(env):
    env.cfg.server.allow_lan = True
    phone = env.client("10.0.0.80")
    phone.post("/api/auth/pair", json={"code": env.gate.issue_code()})
    assert phone.get("/api/ping").status_code == 200
    old = env.token
    new = auth.rotate_token(env.cfg.data_path)
    assert new != old
    assert phone.get("/api/ping").status_code == 401                       # cookie revoked
    c = env.client("10.0.0.90")
    assert c.get("/api/ping", headers={"X-Jarvis-Token": old}).status_code == 401
    assert c.get("/api/ping", headers={"X-Jarvis-Token": new}).status_code == 200
    env.seen.append(new)  # keep the leak assertion below honest for the new token too


def test_rotate_burns_outstanding_pairing_codes(env):
    env.cfg.server.allow_lan = True
    code = env.gate.issue_code()
    auth.rotate_token(env.cfg.data_path)
    assert env.client(LAN_IP).post("/api/auth/pair", json={"code": code}).status_code == 401


# ----------------------------------------------------------------- token file + CLI
def test_ensure_token_is_idempotent_and_private(tmp_path):
    d = tmp_path / "d"
    t1 = auth.ensure_token(d)
    t2 = auth.ensure_token(d)
    assert t1 == t2
    p = auth.token_path(d)
    if os.name != "nt":
        assert (p.stat().st_mode & 0o777) == 0o600
    assert not list(d.glob(".*.tmp"))        # atomic write leaves no temp file


def test_cli_never_prints_token_unless_asked(tmp_path, capsys):
    d = tmp_path / "d"
    assert auth.cli_main(["--data-dir", str(d), "token"]) == 0
    out = capsys.readouterr().out
    token = auth.read_token(d)
    assert token not in out and auth.token_fingerprint(token) in out
    assert auth.cli_main(["--data-dir", str(d), "pair", "--ttl", "120"]) == 0
    out = capsys.readouterr().out
    code = out.split("pairing code:")[1].split()[0]
    assert token not in out
    assert auth.redeem_pairing_code(d, code) is True
    assert auth.redeem_pairing_code(d, code) is False
    assert auth.cli_main(["--data-dir", str(d), "status"]) == 0
    assert token not in capsys.readouterr().out


# ----------------------------------------------------------------- real app integration
def test_real_app_is_gated(kernel):
    """create_app() installs the gate: REST and /ws refuse remote clients by default."""
    from jarvis.api import create_app
    app = create_app(kernel)
    assert getattr(app.state, "auth_gate", None) is not None
    remote = TestClient(app, client=(LAN_IP, 50000))
    r = remote.get("/api/health")
    assert r.status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with remote.websocket_connect("/ws"):
            pass
    assert remote.get("/static/index.html").status_code == 403
    token = auth.read_token(kernel.config.data_path)
    assert token and token not in r.text


# ----------------------------------------------------------------- fail closed + audit
def test_gate_exception_denies_never_allows(env, monkeypatch):
    def boom(scope):
        raise RuntimeError("simulated gate bug")
    monkeypatch.setattr(env.gate, "classify", boom)
    r = env.client("127.0.0.1").get("/api/ping")      # even loopback is denied on a gate bug
    assert r.status_code == 403 and r.json()["reason"] == "gate_error"


def test_every_denial_is_audited_and_pairing_success_audited(env):
    env.cfg.server.allow_lan = True
    n_before = len(_audit_reasons(env.db))
    c = env.client(LAN_IP)
    for _ in range(7):
        assert c.get("/api/ping").status_code == 401
    assert len(_audit_reasons(env.db)) - n_before == 7
    code = env.gate.issue_code()
    assert c.post("/api/auth/pair", json={"code": code}).status_code == 200
    rows = env.db.query("SELECT action, detail FROM audit WHERE action LIKE 'auth.pair%'")
    actions = [r["action"] for r in rows]
    assert "auth.paired" in actions and "auth.pair_code_issued" in actions
    assert all(code not in (r["detail"] or "") and code.replace("-", "") not in (r["detail"] or "")
               for r in rows)


def test_audit_flood_is_capped(env, monkeypatch):
    monkeypatch.setattr(auth, "AUDIT_MAX_PER_MINUTE", 5)
    env.cfg.server.allow_lan = True
    c = env.client(LAN_IP)
    for _ in range(20):
        c.get("/api/ping")
    assert len(_audit_reasons(env.db)) == 5
    assert env.db.query_one("SELECT COUNT(*) AS n FROM audit WHERE action='auth.denied_flood'")["n"] == 1
    env.clock.t += 61
    c.get("/api/ping")
    row = env.db.query_one("SELECT detail FROM audit WHERE action='auth.denied_suppressed'")
    assert row is not None and '"suppressed": 15' in row["detail"]


# ----------------------------------------------------------------- the leak check
def test_token_never_leaks_into_responses_logs_or_audit(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.cfg.server.allow_lan = True
    c = env.client(LAN_IP)
    env.record(c.get("/api/ping"))
    env.record(c.get("/api/ping", headers={"Authorization": f"Bearer {env.token}"}))
    env.record(c.get("/api/auth/status", headers={"Authorization": f"Bearer {env.token}"}))
    env.record(env.client().get("/api/auth/status"))
    env.record(c.post("/api/auth/pair", json={"code": env.gate.issue_code()}))
    env.record(c.get("/api/ping", headers={"Authorization": "Bearer wrong"}))
    env.record(c.get("/pair"))
    for i in range(auth.MAX_FAILURES + 1):
        env.record(env.client("10.0.3.3").get("/api/ping", headers={"X-Jarvis-Token": "bad"}))
    blob = "\n".join(env.seen) + caplog.text
    blob += "\n".join(str(dict(r)) for r in env.db.query("SELECT * FROM audit"))
    assert env.token not in blob
    assert auth.session_cookie_value(env.token) != env.token
