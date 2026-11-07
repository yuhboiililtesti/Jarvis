"""Policy: permission broker, DEFCON posture and the approval gate.

The broker answers one question for every tool call:

    Decision(allow: bool, reason: str, needs_approval: bool)

Rules, in order:
  1. Explicit deny list always wins.
  2. Read-only tools allowed by policy (auto_allow list, or mode=autonomous).
  3. A live grant matching the tool+target allows without prompting.
  4. Otherwise -> needs human approval (mode=ask) or denied (mode=readonly).
  5. DEFCON posture can tighten but never loosen the configured mode.
"""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from typing import Any

from ..ids import new_id, now_iso
from ..logging_setup import get_logger
from ..tools.base import SideEffect

log = get_logger("jarvis.policy")

__all__ = ["Decision", "PolicyBroker", "DEFCON_EFFECTS"]

# How each DEFCON level constrains autonomy. Level 1 = lock down.
DEFCON_EFFECTS: dict[int, dict[str, Any]] = {
    5: {"max_mode": "autonomous", "allow_network_write": True, "note": "normal"},
    4: {"max_mode": "autonomous", "allow_network_write": True, "note": "heightened awareness"},
    3: {"max_mode": "ask", "allow_network_write": True, "note": "elevated; writes need approval"},
    2: {"max_mode": "ask", "allow_network_write": False, "note": "serious; no new remote effects"},
    1: {"max_mode": "readonly", "allow_network_write": False, "note": "lockdown; read-only only"},
}

_MODE_RANK = {"readonly": 0, "ask": 1, "autonomous": 2}


@dataclass
class Decision:
    allow: bool
    reason: str = ""
    needs_approval: bool = False
    grant_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"allow": self.allow, "reason": self.reason,
                "needs_approval": self.needs_approval, "grant_id": self.grant_id}


class PolicyBroker:
    def __init__(self, config: Any, db: Any, bus: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.bus = bus
        # Grants that last only for this Core process. They are deliberately
        # not written to SQLite: a restart is the natural "clear my session
        # approvals" boundary and keeps a mistaken broad allow from becoming
        # permanent. Persistent grants remain the explicit ttl/always path.
        self._session_grants: list[dict[str, Any]] = []

    # -- posture ---------------------------------------------------------
    @property
    def defcon(self) -> int:
        try:
            return int(self.db.kv_get("defcon", self.config.security.defcon))
        except Exception:
            return 5

    def set_defcon(self, level: int) -> None:
        level = max(1, min(5, int(level)))
        self.db.kv_set("defcon", level)
        self.db.audit("defcon.change", actor="owner", detail={"level": level},
                      severity="warn" if level <= 3 else "info")
        if self.bus:
            self.bus.publish("defcon", level=level, note=DEFCON_EFFECTS[level]["note"])

    @property
    def effective_mode(self) -> str:
        configured = self.config.policy.mode
        ceiling = DEFCON_EFFECTS[self.defcon]["max_mode"]
        return configured if _MODE_RANK[configured] <= _MODE_RANK[ceiling] else ceiling

    # -- grants ----------------------------------------------------------
    def add_grant(self, scope: str, *, target: str | None = None,
                  ttl_s: float | None = None, max_uses: int | None = None,
                  note: str = "") -> str:
        gid = new_id("grt")
        expires = None
        if ttl_s:
            expires = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + ttl_s))
        self.db.execute(
            "INSERT INTO grants(id,created_at,expires_at,scope,target,granted_to,"
            "max_uses,note) VALUES(?,?,?,?,?,?,?,?)",
            (gid, now_iso(), expires, scope, target, "owner", max_uses, note),
        )
        self.db.audit("grant.add", actor="owner", target=scope,
                      detail={"grant_id": gid, "target": target, "note": note})
        return gid

    def add_session_grant(self, scope: str, *, target: str | None = None,
                          note: str = "") -> str:
        """Allow a tool until this Core process exits.

        Session grants are intentionally not persisted. They give the owner a
        fast "stop asking me for this one" path without silently creating a
        permanent opening that survives a restart.
        """
        gid = new_id("grs")
        self._session_grants.append({
            "id": gid,
            "created_at": now_iso(),
            "expires_at": None,
            "scope": scope,
            "target": target,
            "granted_to": "owner",
            "max_uses": None,
            "used_count": 0,
            "revoked": 0,
            "note": note,
            "session": True,
        })
        self.db.audit("grant.add_session", actor="owner", target=scope,
                      detail={"grant_id": gid, "target": target, "note": note})
        return gid

    def revoke_grant(self, grant_id: str) -> bool:
        for index, grant in enumerate(self._session_grants):
            if grant["id"] == grant_id:
                self._session_grants.pop(index)
                self.db.audit("grant.revoke_session", actor="owner", target=grant_id)
                return True
        cur = self.db.execute("UPDATE grants SET revoked=1 WHERE id=?", (grant_id,))
        self.db.audit("grant.revoke", actor="owner", target=grant_id)
        return cur.rowcount > 0

    def active_grants(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM grants WHERE revoked=0 ORDER BY created_at DESC")
        out = [dict(grant) for grant in self._session_grants]
        now = now_iso()
        for r in rows:
            if r["expires_at"] and r["expires_at"] < now:
                continue
            if r["max_uses"] is not None and r["used_count"] >= r["max_uses"]:
                continue
            out.append(dict(r))
        return out

    def _find_grant(self, tool: str, target: str | None) -> str | None:
        for g in self.active_grants():
            scope_ok = fnmatch.fnmatch(tool, g["scope"]) or g["scope"] == tool
            # A target-scoped grant must not widen to a call that names no
            # target: "may write C:\safe\*" is not "may write anything".
            target_ok = (not g["target"]) or (
                bool(target) and fnmatch.fnmatch(str(target), g["target"]))
            if scope_ok and target_ok:
                return g["id"]
        return None

    # -- the gate --------------------------------------------------------
    def decide(self, tool: str, side_effect: SideEffect,
               args: dict[str, Any] | None = None) -> Decision:
        cfg = self.config.policy
        args = args or {}
        target = _target_of(args)

        # 1. explicit deny always wins
        if any(fnmatch.fnmatch(tool, d) for d in cfg.always_deny):
            return Decision(False, f"'{tool}' is on the deny list")

        # 2. DEFCON can suspend outbound effects regardless of mode
        if not DEFCON_EFFECTS[self.defcon]["allow_network_write"] and \
                side_effect in (SideEffect.REMOTE, SideEffect.NETWORK):
            return Decision(False, f"DEFCON {self.defcon}: outbound effects suspended")

        mode = self.effective_mode

        # 3. read-only posture refuses every mutation
        if mode == "readonly" and side_effect != SideEffect.NONE:
            return Decision(False, "read-only mode: mutating actions disabled")

        # 4. an explicit owner allow-list entry permits the tool as-is.
        #    This is an owner decision, so it applies to any side-effect class
        #    (checks 1-3 above still outrank it).
        if any(fnmatch.fnmatch(tool, a) for a in cfg.auto_allow):
            return Decision(True, "listed in policy.auto_allow")

        # 5. a live scoped grant permits without prompting
        grant_id = self._find_grant(tool, target)
        if grant_id:
            return Decision(True, f"covered by standing grant {grant_id}", grant_id=grant_id)

        # 6. autonomous mode runs everything except destructive acts
        if mode == "autonomous" and side_effect != SideEffect.DESTRUCTIVE:
            return Decision(True, "autonomous mode")

        return Decision(False, f"'{tool}' ({side_effect.value}) requires approval",
                        needs_approval=True)

    def consume_grant(self, grant_id: str | None) -> None:
        if not grant_id:
            return
        self.db.execute("UPDATE grants SET used_count = used_count + 1 WHERE id=?", (grant_id,))


def _target_of(args: dict[str, Any]) -> str | None:
    for key in ("path", "target", "url", "node", "host", "name", "command"):
        if key in args and args[key]:
            return str(args[key])
    return None
