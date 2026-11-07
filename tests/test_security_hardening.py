"""Security-hardening regressions: the fixes from the external audit.

Covers:
  * policy is re-evaluated immediately before an effect (no TOCTOU across the
    approval wait),
  * remembered approvals are scoped to their target, not the tool name alone,
  * authority-governing routes require a credential even on loopback,
  * the loopback unlock endpoint sets the owner session cookie.
"""

from __future__ import annotations

import asyncio

import pytest


# ----------------------------------------------------------------- recheck ----

@pytest.mark.asyncio
async def test_policy_change_during_approval_blocks_action(kernel, register_test_tools,
                                                           tmp_path):
    target = tmp_path / "blocked.txt"
    t = kernel.controller.create("do a gated thing")

    async def run():
        return await kernel.controller.execute_tool(t, "test.gated",
                                                    {"path": str(target)})

    task = asyncio.create_task(run())
    for _ in range(200):
        if kernel.controller.pending_approvals():
            break
        await asyncio.sleep(0.01)
    assert kernel.controller.pending_approvals(), "no approval was requested"

    # The owner approves, but the posture tightens to read-only during the wait.
    kernel.policy.set_defcon(1)  # DEFCON 1 = readonly
    kernel.controller.resolve_approval(
        kernel.controller.pending_approvals()[0]["id"], "allow")

    result, _ = await task
    assert not result.ok, "a mutating action ran after the posture went read-only"
    assert not target.exists(), "gated tool executed despite the recheck"


# ----------------------------------------------------------------- grants -----

@pytest.mark.asyncio
async def test_remembered_approval_scopes_to_target(kernel, register_test_tools,
                                                    tmp_path):
    target = tmp_path / "allowed.txt"
    t = kernel.controller.create("do a gated thing")

    async def run():
        return await kernel.controller.execute_tool(t, "test.gated",
                                                    {"path": str(target)})

    task = asyncio.create_task(run())
    for _ in range(200):
        if kernel.controller.pending_approvals():
            break
        await asyncio.sleep(0.01)

    kernel.controller.resolve_approval(
        kernel.controller.pending_approvals()[0]["id"], "allow",
        remember_session=True)
    result, _ = await task
    assert result.ok

    grants = kernel.policy.active_grants()
    assert grants, "remembered approval did not create a grant"
    assert any(str(g["target"]) == str(target) for g in grants), \
        "remembered approval was not scoped to its target"


# ----------------------------------------------------------------- loopback ----

def test_protected_routes_require_token_on_loopback(kernel):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import create_app
    from jarvis.api.auth import read_token
    app = create_app(kernel)
    token = read_token(kernel.config.data_path)
    with fastapi_testclient.TestClient(app) as client:
        # Authority-governing routes must not be free on loopback.
        assert client.post("/api/defcon", json={"level": 3}).status_code == 401
        assert client.post("/api/settings", json={"policy.mode": "ask"}).status_code == 401
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/grants").status_code == 401
        # Non-authority routes remain loopback-open.
        assert client.get("/api/health").status_code == 200
        # A valid token unlocks them.
        assert client.post("/api/defcon", json={"level": 3},
                           headers={"X-Jarvis-Token": token}).status_code == 200


def test_unlock_endpoint_sets_session_cookie(kernel):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import create_app
    from jarvis.api.auth import read_token
    app = create_app(kernel)
    token = read_token(kernel.config.data_path)
    with fastapi_testclient.TestClient(app) as client:
        # Wrong token is refused.
        assert client.post("/api/auth/unlock",
                           json={"token": "wrong"}).status_code == 401
        # Correct token sets the cookie; subsequent requests authenticate via it.
        r = client.post("/api/auth/unlock", json={"token": token})
        assert r.status_code == 200
        assert client.post("/api/defcon", json={"level": 3}).status_code == 200
