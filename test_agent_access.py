"""Slice-1 Step 1 proof: scoped agent access grants + deny-by-default + audit.

Run: python -m pytest test_agent_access.py -q
"""
import os
import time
import tempfile
import shutil

import pytest

from decisiongraph.agent_access import AgentAccessStore, AgentAccessError


@pytest.fixture()
def ws_root():
    d = tempfile.mkdtemp(prefix="dg_ws_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_mint_creates_scoped_grant(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("billing-bot", ["Pricing", "Refunds"], ttl_seconds=60)
    assert g["token"].startswith("ag_")
    assert g["allowed_topics"] == ["pricing", "refunds"]  # normalized + sorted
    assert g["can_write"] is False
    assert s.get(g["token"]) is not None


def test_in_scope_read_allowed(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["pricing"])
    assert s.authorize(g["token"], "Pricing", "read")["agent_name"] == "bot"


def test_out_of_scope_denied_and_logged(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["pricing"])
    with pytest.raises(AgentAccessError):
        s.authorize(g["token"], "salaries", "read")
    audit = s.read_audit()
    assert any(r["event"] == "access_denied" and r["topic"] == "salaries"
               and r["reason"] == "topic out of scope" for r in audit)


def test_unknown_token_denied_and_logged(ws_root):
    s = AgentAccessStore(ws_root)
    with pytest.raises(AgentAccessError):
        s.authorize("ag_not_real", "pricing")
    assert any(r["event"] == "access_denied" and r["reason"] == "unknown agent token"
               for r in s.read_audit())


def test_revoked_grant_fails_closed(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["pricing"])
    assert s.authorize(g["token"], "pricing")  # works first
    assert s.revoke(g["token"]) is True
    with pytest.raises(AgentAccessError):
        s.authorize(g["token"], "pricing")
    assert any(r["event"] == "access_denied" and r["reason"] == "grant revoked"
               for r in s.read_audit())


def test_expired_grant_fails_closed(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["pricing"], ttl_seconds=1)
    time.sleep(1.2)
    with pytest.raises(AgentAccessError):
        s.authorize(g["token"], "pricing")
    assert any(r.get("reason") == "grant expired" for r in s.read_audit())


def test_write_denied_unless_granted(ws_root):
    s = AgentAccessStore(ws_root)
    ro = s.mint("ro", ["pricing"], can_write=False)
    with pytest.raises(AgentAccessError):
        s.authorize(ro["token"], "pricing", "write")
    rw = s.mint("rw", ["pricing"], can_write=True)
    assert s.authorize(rw["token"], "pricing", "write")["can_write"] is True


def test_empty_allowlist_denies_everything(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", [])
    with pytest.raises(AgentAccessError):
        s.authorize(g["token"], "anything")


def test_audit_is_append_only(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["pricing"])
    s.authorize(g["token"], "pricing")
    try:
        s.authorize(g["token"], "secret")
    except AgentAccessError:
        pass
    audit = s.read_audit()
    events = [r["event"] for r in audit]
    # mint + granted + denied all present, in order, nothing overwritten
    assert events == ["grant_minted", "access_granted", "access_denied"]


def test_isolation_two_workspaces_dont_share(ws_root):
    a = AgentAccessStore(ws_root)
    other = ws_root + "_b"
    os.makedirs(other, exist_ok=True)
    try:
        b = AgentAccessStore(other)
        ga = a.mint("a-bot", ["pricing"])
        # token minted in workspace A must not resolve in workspace B
        assert b.get(ga["token"]) is None
        with pytest.raises(AgentAccessError):
            b.authorize(ga["token"], "pricing")
    finally:
        shutil.rmtree(other, ignore_errors=True)
