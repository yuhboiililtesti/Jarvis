"""notify.toast (command mocked), home.* (fake Home Assistant over httpx.MockTransport)
and email.draft / email.send (fake SMTP). No network, no real toast, no real mail."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import SideEffect, ToolContext
from jarvis.tools.builtin import _win, email_tool, home_tool, notify_tool


@pytest.fixture
def ctx(cfg):
    return ToolContext(config=cfg, db=None, bus=None)


def test_specs():
    expect = {"notify.toast": SideEffect.LOCAL_EXEC, "home.states": SideEffect.NETWORK,
              "home.call_service": SideEffect.REMOTE,
              "home.call_service_critical": SideEffect.DESTRUCTIVE,
              "email.draft": SideEffect.LOCAL_WRITE, "email.send": SideEffect.REMOTE}
    for name, side in expect.items():
        spec = REGISTRY.get(name).spec
        assert spec.side_effect == side, name
        names = [p.name for p in spec.params]
        assert "name" not in names
        if side != SideEffect.NETWORK:
            assert "dry_run" in names, name


# ----------------------------------------------------------------- notify ---

@pytest.fixture
def cmd(monkeypatch):
    calls = []

    def fake(argv, timeout=15, env=None, input_text=None):
        calls.append({"argv": argv, "env": env})
        return {"ok": True, "code": 0, "stdout": "shown", "stderr": "", "error": None}
    monkeypatch.setattr(_win, "run_cmd", fake)
    monkeypatch.setattr(notify_tool, "health", lambda: {"available": True, "reason": "t"})
    return calls


async def test_toast_passes_text_via_env_not_script(ctx, cmd, monkeypatch):
    monkeypatch.setattr(_win, "IS_WINDOWS", True)
    evil = "'; Remove-Item C:\\ -Recurse; '<toast>"
    tool = REGISTRY.get("notify.toast")
    r = await tool.run(ctx, title=evil, message="body\x07text")
    assert r.ok and r.certainty == "unverified"
    call = cmd[0]
    assert call["argv"][0] == "powershell"
    assert evil not in " ".join(call["argv"])
    assert call["env"]["JARVIS_TOAST_TITLE"] == evil
    assert call["env"]["JARVIS_TOAST_BODY"] == "body text"
    assert "CreateTextNode($env:JARVIS_TOAST_TITLE)" in notify_tool.TOAST_SCRIPT
    assert tool.verify(ctx, r) is None


async def test_toast_linux_and_validation(ctx, cmd, monkeypatch):
    monkeypatch.setattr(_win, "IS_WINDOWS", False)
    r = await REGISTRY.get("notify.toast").run(ctx, title="Hi", message="there")
    assert r.ok and cmd[-1]["argv"] == ["notify-send", "--app-name=JARVIS", "--", "Hi", "there"]
    r = await REGISTRY.get("notify.toast").run(ctx, title="  ")
    assert not r.ok
    r = await REGISTRY.get("notify.toast").run(ctx, title="x" * 500, dry_run=True)
    assert r.ok and r.data["dry_run"] and len(r.data["title"]) == notify_tool.MAX_TITLE
    assert len(cmd) == 1


async def test_toast_failure_reported(ctx, monkeypatch):
    monkeypatch.setattr(notify_tool, "health", lambda: {"available": True, "reason": "t"})
    monkeypatch.setattr(_win, "run_cmd", lambda *a, **k: {
        "ok": False, "code": 1, "stdout": "", "stderr": "WinRT type not found", "error": None})
    r = await REGISTRY.get("notify.toast").run(ctx, title="x")
    assert not r.ok and "WinRT" in r.error


# ------------------------------------------------------------------- home ---

class FakeHA:
    def __init__(self):
        self.states = {
            "light.kitchen": {"entity_id": "light.kitchen", "state": "off",
                              "attributes": {"friendly_name": "Kitchen"},
                              "last_updated": "t0", "last_changed": "t0"},
            "lock.front_door": {"entity_id": "lock.front_door", "state": "locked",
                                "attributes": {"friendly_name": "Front door"},
                                "last_updated": "t0", "last_changed": "t0"},
            "cover.garage": {"entity_id": "cover.garage", "state": "closed",
                             "attributes": {"device_class": "garage"},
                             "last_updated": "t0", "last_changed": "t0"},
            "cover.blinds": {"entity_id": "cover.blinds", "state": "open",
                             "attributes": {"device_class": "blind"},
                             "last_updated": "t0", "last_changed": "t0"},
            "switch.oven": {"entity_id": "switch.oven", "state": "off",
                            "attributes": {"friendly_name": "Oven"},
                            "last_updated": "t0", "last_changed": "t0"},
            "switch.lamp": {"entity_id": "switch.lamp", "state": "off",
                            "attributes": {"friendly_name": "Desk lamp"},
                            "last_updated": "t0", "last_changed": "t0"},
        }
        self.posts = []
        self.auth = []
        self.apply = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization"))
        path = request.url.path
        if request.method == "GET" and path == "/api/states":
            return httpx.Response(200, json=list(self.states.values()))
        if request.method == "GET" and path.startswith("/api/states/"):
            st = self.states.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=st) if st else httpx.Response(404)
        if request.method == "POST" and path.startswith("/api/services/"):
            _, _, _, domain, service = path.split("/")
            body = json.loads(request.content or b"{}")
            self.posts.append((domain, service, body))
            if self.apply:
                ids = body.get("entity_id")
                ids = [ids] if isinstance(ids, str) else ids or []
                new = {"turn_on": "on", "turn_off": "off", "unlock": "unlocked",
                       "lock": "locked", "open_cover": "open"}.get(service)
                for eid in ids:
                    if new:
                        self.states[eid]["state"] = new
                    self.states[eid]["last_updated"] = "t1"
            return httpx.Response(200, json=[])
        return httpx.Response(404)


@pytest.fixture
def ha(cfg, ctx):
    fake = FakeHA()
    home_tool.set_transport(httpx.MockTransport(fake.handler))
    cfg.integrations.home_assistant_url = "http://ha.local:8123"
    ctx.secrets = lambda key: "TOKEN123" if key == "home_assistant" else None
    yield fake
    home_tool.set_transport(None)


async def test_home_not_configured(ctx):
    r = await REGISTRY.call("home.states", ctx)
    assert not r.ok and "not configured" in r.error
    r = await REGISTRY.get("home.call_service").run(ctx, domain="light", service="turn_on")
    assert not r.ok and "not configured" in r.error


async def test_home_states(ctx, ha):
    r = await REGISTRY.call("home.states", ctx, domain="cover")
    assert r.ok and {e["entity_id"] for e in r.data["entities"]} == {"cover.garage", "cover.blinds"}
    r = await REGISTRY.call("home.states", ctx, entity_id="light.kitchen")
    assert r.ok and r.data["entities"][0]["state"] == "off"
    assert set(ha.auth) == {"Bearer TOKEN123"}
    assert "TOKEN123" not in json.dumps(r.data)
    r = await REGISTRY.call("home.states", ctx, entity_id="light.nope")
    assert not r.ok
    r = await REGISTRY.call("home.states", ctx, entity_id="bad id!")
    assert not r.ok


async def test_light_call_and_readback(ctx, ha):
    tool = REGISTRY.get("home.call_service")
    r = await tool.run(ctx, domain="light", service="turn_on", entity_id="light.kitchen",
                       data={"brightness_pct": 40})
    assert r.ok and r.data["impact"] == "low"
    assert ha.posts == [("light", "turn_on", {"brightness_pct": 40, "entity_id": "light.kitchen"})]
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["observed"]["light.kitchen"]["state"] == "on"


async def test_readback_detects_device_that_did_not_change(ctx, ha, monkeypatch):
    monkeypatch.setattr(home_tool.asyncio, "sleep", _no_sleep)
    ha.apply = False
    tool = REGISTRY.get("home.call_service")
    r = await tool.run(ctx, domain="light", service="turn_on", entity_id="light.kitchen")
    v = await tool.verify(ctx, r)
    assert v["verified"] is False


async def _no_sleep(_s):
    return None


@pytest.mark.parametrize("kw,reason", [
    ({"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"}, "domain 'lock'"),
    ({"domain": "cover", "service": "open_cover", "entity_id": "cover.garage"}, "garage"),
    ({"domain": "switch", "service": "turn_on", "entity_id": "switch.oven"}, "heating"),
    ({"domain": "switch", "service": "turn_off"}, "every switch"),
    ({"domain": "climate", "service": "set_temperature"}, "climate"),
    ({"domain": "script", "service": "turn_on"}, "script"),
    ({"domain": "light", "service": "turn_on", "entity_id": "lock.front_door"}, "lock"),
])
async def test_high_impact_refused_by_low_tool(ctx, ha, kw, reason):
    r = await REGISTRY.get("home.call_service").run(ctx, **kw)
    assert not r.ok and "high-impact" in r.error and reason in r.error
    assert r.evidence.get("refused") and ha.posts == []


async def test_low_impact_switch_and_blinds_allowed(ctx, ha):
    tool = REGISTRY.get("home.call_service")
    assert (await tool.run(ctx, domain="switch", service="turn_on",
                           entity_id="switch.lamp")).ok
    assert (await tool.run(ctx, domain="cover", service="open_cover",
                           entity_id="cover.blinds")).ok


async def test_critical_tool_handles_lock_with_readback_and_dry_run(ctx, ha):
    tool = REGISTRY.get("home.call_service_critical")
    r = await tool.run(ctx, domain="lock", service="unlock", entity_id="lock.front_door",
                       dry_run=True)
    assert r.ok and r.data["dry_run"] and r.data["impact"] == "high" and ha.posts == []
    assert r.data["target"][0]["state"] == "locked"
    r = await tool.run(ctx, domain="lock", service="unlock", entity_id="lock.front_door")
    assert r.ok and ha.posts[0][:2] == ("lock", "unlock")
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["observed"]["lock.front_door"]["state"] == "unlocked"


async def test_home_input_validation(ctx, ha):
    tool = REGISTRY.get("home.call_service")
    for kw in ({"domain": "light!", "service": "turn_on"},
               {"domain": "light", "service": "turn on"},
               {"domain": "light", "service": "turn_on", "entity_id": "kitchen"},
               {"domain": "light", "service": "turn_on", "data": "x"},
               {"domain": "light", "service": "turn_on", "data": {"entity_id": "lock.x"}},
               {"domain": "light", "service": "turn_on", "entity_id": "light.ghost"}):
        r = await tool.run(ctx, **kw)
        assert not r.ok, kw
    assert ha.posts == []


def test_classify_call_unit():
    assert home_tool.classify_call("light", "turn_on", ["light.a"], {})[0] == "low"
    assert home_tool.classify_call("alarm_control_panel", "alarm_disarm", [], {})[0] == "high"
    assert home_tool.classify_call("valve", "open_valve", ["valve.main"], {})[0] == "high"
    assert home_tool.classify_call(
        "cover", "open_cover", ["cover.g"],
        {"cover.g": {"attributes": {"device_class": "gate"}}})[0] == "high"


# ------------------------------------------------------------------ email ---

async def test_draft_writes_eml_and_verifies(ctx, cfg):
    tool = REGISTRY.get("email.draft")
    r = await tool.run(ctx, to=["Ann <ann@example.com>"], subject="Hello", body="Line 1\nLine 2",
                       cc="bob@example.org")
    assert r.ok and r.data["sent"] is False
    path = Path(r.data["path"])
    assert path.parent == Path(cfg.data_path) / "artifacts" / "drafts"
    import email as _email
    from email import policy as _policy
    parsed = _email.message_from_bytes(path.read_bytes(), policy=_policy.default)
    assert parsed["To"] == "ann@example.com" and parsed["Cc"] == "bob@example.org"
    body = parsed.get_body(("plain",)).get_content()
    assert body.replace("\r\n", "\n").strip() == "Line 1\nLine 2"
    assert (await tool.verify(ctx, r))["verified"] is True
    path.write_bytes(path.read_bytes().replace(b"Hello", b"Hacked"))
    assert (await tool.verify(ctx, r))["verified"] is False


async def test_draft_validation_and_header_injection(ctx, cfg):
    tool = REGISTRY.get("email.draft")
    for kw in ({"to": "not-an-address", "subject": "x"},
               {"to": "a@example.com", "subject": "x\r\nBcc: evil@example.com"},
               {"to": [f"u{i}@example.com" for i in range(21)], "subject": "x"},
               {"subject": "x"}):
        r = await tool.run(ctx, **kw)
        assert not r.ok, kw
    drafts = Path(cfg.data_path) / "artifacts" / "drafts"
    assert not drafts.exists() or not list(drafts.iterdir())
    r = await tool.run(ctx, to="a@example.com", subject="x", dry_run=True)
    assert r.ok and r.data["dry_run"] and not Path(r.data["target"]).exists()


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    starttls_offered = True
    refuse: dict = {}

    def __init__(self, host, port, timeout=30, context=None):
        self.host, self.port = host, port
        self.log = []
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.log.append("ehlo")

    def has_extn(self, name):
        return self.starttls_offered

    def starttls(self, context=None):
        self.log.append("starttls")

    def login(self, user, password):
        self.log.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.log.append(("send", from_addr, list(to_addrs), msg["Subject"]))
        return dict(self.refuse)

    def quit(self):
        self.log.append("quit")


@pytest.fixture
def smtp(cfg, ctx, monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.starttls_offered = True
    FakeSMTP.refuse = {}
    monkeypatch.setattr(email_tool, "_SMTP_FACTORY", FakeSMTP)
    cfg.integrations.smtp_host = "smtp.example.com"
    cfg.integrations.smtp_user = "me@example.com"
    ctx.secrets = lambda key: "pw-secret" if key == "smtp" else None
    return FakeSMTP


async def test_send_refuses_when_not_configured(ctx):
    r = await REGISTRY.get("email.send").run(ctx, to="a@example.com", subject="x", body="y")
    assert not r.ok and "not configured" in r.error and "smtp_host" in r.error


async def test_send_fresh_message_starttls(ctx, smtp):
    tool = REGISTRY.get("email.send")
    r = await tool.run(ctx, to="a@example.com", cc=["b@example.com"], subject="Hi", body="Yo")
    assert r.ok and r.data["accepted"] == ["a@example.com", "b@example.com"]
    log = smtp.instances[0].log
    assert log.index("starttls") < log.index(("login", "me@example.com", "pw-secret"))
    assert ("send", "me@example.com", ["a@example.com", "b@example.com"], "Hi") in log
    assert "pw-secret" not in json.dumps(r.data) and "pw-secret" not in json.dumps(r.evidence)
    assert tool.verify(ctx, r) is None


async def test_send_aborts_without_starttls(ctx, smtp):
    smtp.starttls_offered = False
    r = await REGISTRY.get("email.send").run(ctx, to="a@example.com", subject="x", body="y")
    assert not r.ok and "STARTTLS" in r.error
    assert not any(isinstance(e, tuple) and e[0] == "login" for e in smtp.instances[0].log)


async def test_send_existing_draft_and_draft_path_guard(ctx, smtp, tmp_path):
    d = await REGISTRY.get("email.draft").run(ctx, to="c@example.com", subject="From draft",
                                             body="b")
    name = Path(d.data["path"]).name
    r = await REGISTRY.get("email.send").run(ctx, draft=name)
    assert r.ok and r.data["subject"] == "From draft"
    outside = tmp_path / "evil.eml"
    outside.write_text("To: x@example.com\nSubject: s\n\nb", encoding="utf-8")
    r = await REGISTRY.get("email.send").run(ctx, draft=str(outside))
    assert not r.ok and "refused" in r.error
    assert len(smtp.instances) == 1


async def test_send_dry_run_and_partial_refusal(ctx, smtp):
    tool = REGISTRY.get("email.send")
    r = await tool.run(ctx, to="a@example.com", subject="x", body="y", dry_run=True)
    assert r.ok and r.data["dry_run"] and r.data["reversible"] is False
    assert smtp.instances == []
    smtp.refuse = {"a@example.com": (550, b"no such user")}
    r = await tool.run(ctx, to=["a@example.com", "b@example.com"], subject="x", body="y")
    assert not r.ok and "refused" in r.error and r.data["accepted"] == ["b@example.com"]
    assert len(smtp.instances) == 1                    # exactly one attempt, no retry


def test_every_new_module_exposes_health():
    import importlib
    for mod in ("window_tool", "input_tool", "media_tool", "power_tool", "process_tool",
                "file_ops", "weather_tool", "browser_tool", "ffmpeg_tool", "docs_tool",
                "notify_tool", "home_tool", "email_tool"):
        h = importlib.import_module(f"jarvis.tools.builtin.{mod}").health()
        assert isinstance(h, dict) and isinstance(h.get("available"), bool) and h.get("reason"), mod
