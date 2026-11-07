"""Policy broker: read-only auto-allow, grants, DEFCON clamping, deny list."""

from __future__ import annotations

from jarvis.policy import PolicyBroker
from jarvis.tools.base import SideEffect


def make(db, cfg):
    return PolicyBroker(cfg, db)


def test_readonly_autolisted_is_allowed(db, cfg):
    p = make(db, cfg)
    d = p.decide("system.info", SideEffect.NONE, {})
    assert d.allow and not d.needs_approval


def test_unlisted_readonly_needs_approval_in_ask_mode(db, cfg):
    p = make(db, cfg)
    d = p.decide("something.else", SideEffect.NONE, {})
    assert not d.allow and d.needs_approval


def test_mutation_needs_approval_in_ask_mode(db, cfg):
    p = make(db, cfg)
    d = p.decide("files.write", SideEffect.LOCAL_WRITE, {"path": "x"})
    assert not d.allow and d.needs_approval


def test_readonly_mode_blocks_all_mutations(db, cfg):
    cfg.policy.mode = "readonly"
    p = make(db, cfg)
    d = p.decide("files.write", SideEffect.LOCAL_WRITE, {})
    assert not d.allow and not d.needs_approval


def test_autonomous_mode_allows_nondestructive(db, cfg):
    cfg.policy.mode = "autonomous"
    p = make(db, cfg)
    assert p.decide("files.write", SideEffect.LOCAL_WRITE, {}).allow
    # destructive still gated
    d = p.decide("files.delete", SideEffect.DESTRUCTIVE, {})
    assert not d.allow and d.needs_approval


def test_standing_grant_allows(db, cfg):
    p = make(db, cfg)
    p.add_grant("files.write", ttl_s=60)
    d = p.decide("files.write", SideEffect.LOCAL_WRITE, {})
    assert d.allow and d.grant_id


def test_target_scoped_grant_does_not_widen_to_a_targetless_call(db, cfg):
    p = make(db, cfg)
    p.add_grant("files.write", target=r"C:\safe\*", ttl_s=60)
    # No target in the call: the grant cannot prove the call is in scope.
    assert not p.decide("files.write", SideEffect.LOCAL_WRITE, {}).allow
    # The named scope still works, and a different target is still refused.
    assert p.decide("files.write", SideEffect.LOCAL_WRITE,
                    {"path": r"C:\safe\notes.txt"}).allow
    assert not p.decide("files.write", SideEffect.LOCAL_WRITE,
                        {"path": r"C:\other\notes.txt"}).allow


def test_session_grant_allows_and_can_be_revoked(db, cfg):
    p = make(db, cfg)
    gid = p.add_session_grant("files.write", note="approval test")
    d = p.decide("files.write", SideEffect.LOCAL_WRITE, {})
    assert d.allow and d.grant_id == gid
    assert any(g["id"] == gid and g.get("session") for g in p.active_grants())
    assert p.revoke_grant(gid)
    assert not p.decide("files.write", SideEffect.LOCAL_WRITE, {}).allow


def test_session_grant_does_not_survive_a_new_policy_instance(db, cfg):
    first = make(db, cfg)
    first.add_session_grant("files.write")
    assert first.decide("files.write", SideEffect.LOCAL_WRITE, {}).allow
    second = make(db, cfg)
    assert not second.decide("files.write", SideEffect.LOCAL_WRITE, {}).allow


def test_expired_grant_does_not_allow(db, cfg):
    p = make(db, cfg)
    p.add_grant("files.write", ttl_s=-1)
    d = p.decide("files.write", SideEffect.LOCAL_WRITE, {})
    assert not d.allow


def test_deny_list_wins(db, cfg):
    cfg.policy.always_deny.append("files.delete")
    p = make(db, cfg)
    cfg.policy.mode = "autonomous"
    d = p.decide("files.delete", SideEffect.DESTRUCTIVE, {})
    assert not d.allow and not d.needs_approval


def test_defcon_clamps_autonomous_to_ask(db, cfg):
    cfg.policy.mode = "autonomous"
    p = make(db, cfg)
    p.set_defcon(3)
    assert p.effective_mode == "ask"


def test_defcon1_forces_readonly_and_blocks_network(db, cfg):
    p = make(db, cfg)
    p.set_defcon(1)
    assert p.effective_mode == "readonly"
    d = p.decide("web.fetch", SideEffect.NETWORK, {})
    assert not d.allow


def test_defcon_never_loosens(db, cfg):
    cfg.policy.mode = "readonly"
    p = make(db, cfg)
    p.set_defcon(5)
    assert p.effective_mode == "readonly"
