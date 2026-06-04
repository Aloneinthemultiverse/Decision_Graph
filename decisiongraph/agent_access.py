"""Slice-1 Step 1 — Scoped access grants for external agents.

An *agent grant* is a credential, separate from a visitor's workspace cookie,
that lets an external/hired agent read a SPECIFIC company's DecisionGraph
memory but ONLY within an explicit topic allowlist.

Design rules (mirrors workspace.py discipline):
  * stdlib only, thread-safe, NO global state.
  * Everything lives UNDER the workspace's own directory
    (storage/workspaces/<ws_token>/agent/...), so a grant physically cannot
    point at another tenant's data — it inherits workspace isolation.
  * Deny by default: a topic is readable iff it is in `allowed_topics`
    (case-insensitive, exact match on the topic/community label).
  * Every check — ALLOWED or DENIED — is appended to an immutable
    JSONL audit log (agent_audit.jsonl). Append-only; never rewritten.
  * Grants are revocable and expirable. A revoked/expired grant fails
    closed (no access) and the attempt is still logged.

This module deliberately does NOT do sandboxing or egress control — those
are Slice-1 Steps 2-3. This step is purely "who may read what, and prove it".
"""
from __future__ import annotations

import os
import json
import time
import secrets
import threading
from typing import Optional

_GRANTS_FILE = "agent_grants.json"
_AUDIT_FILE = "agent_audit.jsonl"

# one lock per workspace path (grants/audit are tiny; coarse lock is fine)
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(ws_root: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(ws_root)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[ws_root] = lk
        return lk


def _agent_dir(ws_root: str) -> str:
    d = os.path.join(ws_root, "agent")
    os.makedirs(d, exist_ok=True)
    return d


def _norm(topic: str) -> str:
    return (topic or "").strip().lower()


class AgentAccessError(Exception):
    """Raised when a grant is invalid, revoked, expired, or out of scope."""


class AgentAccessStore:
    """File-backed, per-workspace store of agent grants + audit log.

    Construct with the *workspace root directory* (the same `ws.root` used by
    workspace.py). All state stays inside that directory.
    """

    def __init__(self, ws_root: str, encrypt: bool = False,
                 key_path: str | None = None):
        self.ws_root = ws_root
        self._dir = _agent_dir(ws_root)
        self._grants_path = os.path.join(self._dir, _GRANTS_FILE)
        self._audit_path = os.path.join(self._dir, _AUDIT_FILE)
        self._lock = _lock_for(ws_root)
        self._enc = None
        if encrypt:
            from .agent_crypto import FileEncryptor
            self._enc = FileEncryptor(key_path=key_path)

    # ── persistence (transparently encrypted when self._enc is set) ───────
    def _load(self) -> dict:
        try:
            with open(self._grants_path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return {}
        if not raw.strip():
            return {}
        if self._enc is not None:
            try:
                return json.loads(self._enc.decrypt_text(raw))
            except Exception:
                pass  # tolerate a pre-existing plaintext file (migration)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _save(self, grants: dict) -> None:
        payload = json.dumps(grants, indent=2, sort_keys=True)
        tmp = self._grants_path + ".tmp"
        if self._enc is not None:
            with open(tmp, "wb") as f:
                f.write(self._enc.encrypt_text(payload))
        else:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
        os.replace(tmp, self._grants_path)  # atomic

    def _audit(self, record: dict) -> None:
        record = {"ts": time.time(), **record}
        line = json.dumps(record, sort_keys=True)
        if self._enc is not None:
            line = self._enc.encrypt_line(line)
        # append-only; never truncated/rewritten
        with open(self._audit_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ── grant lifecycle ──────────────────────────────────────────────────
    def mint(
        self,
        agent_name: str,
        allowed_topics: list[str],
        ttl_seconds: int = 3600,
        can_write: bool = False,
        is_owner: bool = False,
    ) -> dict:
        """Create a new scoped grant. Returns the full grant incl. its token.

        The token is the agent's bearer credential. allowed_topics is the
        EXACT allowlist (case-insensitive). Empty allowlist => agent can
        read nothing (deny-by-default is literal here).
        """
        if not agent_name or not str(agent_name).strip():
            raise AgentAccessError("agent_name required")
        prefix = "ow_" if is_owner else "ag_"
        token = prefix + secrets.token_urlsafe(18)
        now = time.time()
        grant = {
            "token": token,
            "agent_name": str(agent_name).strip(),
            "allowed_topics": sorted({_norm(t) for t in (allowed_topics or []) if _norm(t)}),
            "can_write": bool(can_write) or bool(is_owner),
            "is_owner": bool(is_owner),
            "created_at": now,
            "expires_at": now + int(ttl_seconds) if ttl_seconds else None,
            "revoked": False,
        }
        with self._lock:
            grants = self._load()
            grants[token] = grant
            self._save(grants)
            self._audit({
                "event": "grant_minted",
                "token": token,
                "agent_name": grant["agent_name"],
                "allowed_topics": grant["allowed_topics"],
                "can_write": grant["can_write"],
                "expires_at": grant["expires_at"],
            })
        return grant

    def get(self, token: str) -> Optional[dict]:
        with self._lock:
            return self._load().get(token)

    def revoke(self, token: str) -> bool:
        with self._lock:
            grants = self._load()
            g = grants.get(token)
            if not g:
                return False
            g["revoked"] = True
            g["revoked_at"] = time.time()
            grants[token] = g
            self._save(grants)
            self._audit({"event": "grant_revoked", "token": token,
                         "agent_name": g.get("agent_name")})
            return True

    def list_grants(self) -> list[dict]:
        with self._lock:
            return list(self._load().values())

    # ── the gate ─────────────────────────────────────────────────────────
    def _validate(self, token: str) -> dict:
        g = self.get(token)
        if not g:
            raise AgentAccessError("unknown agent token")
        if g.get("revoked"):
            raise AgentAccessError("grant revoked")
        exp = g.get("expires_at")
        if exp is not None and time.time() > exp:
            raise AgentAccessError("grant expired")
        return g

    def authorize(self, token: str, topic: str, action: str = "read") -> dict:
        """The single gate. Returns the grant if the (token, topic, action)
        is permitted; raises AgentAccessError otherwise. EVERY call — allowed
        or denied — is written to the immutable audit log.
        """
        topic_n = _norm(topic)
        try:
            g = self._validate(token)
        except AgentAccessError as e:
            self._audit({"event": "access_denied", "token": token,
                         "topic": topic_n, "action": action, "reason": str(e)})
            raise

        if action == "write" and not g.get("can_write"):
            self._audit({"event": "access_denied", "token": token,
                         "agent_name": g.get("agent_name"), "topic": topic_n,
                         "action": action, "reason": "write not permitted"})
            raise AgentAccessError("write not permitted for this grant")

        if topic_n not in set(g.get("allowed_topics", [])):
            self._audit({"event": "access_denied", "token": token,
                         "agent_name": g.get("agent_name"), "topic": topic_n,
                         "action": action, "reason": "topic out of scope"})
            raise AgentAccessError(f"topic '{topic}' is out of scope for this agent")

        self._audit({"event": "access_granted", "token": token,
                     "agent_name": g.get("agent_name"), "topic": topic_n,
                     "action": action})
        return g

    def record_event(self, event: str, **fields) -> None:
        """Append a job-lifecycle event to the SAME immutable audit log.
        Used by the orchestrator (Step 4) for job_started / learning_written /
        job_completed / job_failed entries."""
        with self._lock:
            self._audit({"event": event, **fields})

    def read_audit(self, limit: int = 200) -> list[dict]:
        """Return the most recent audit records (newest last)."""
        try:
            with open(self._audit_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        out = []
        for ln in lines[-limit:]:
            ln = ln.strip()
            if not ln:
                continue
            if self._enc is not None:
                try:
                    ln = self._enc.decrypt_line(ln)
                except Exception:
                    pass  # tolerate a pre-existing plaintext line (migration)
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out
