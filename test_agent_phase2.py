"""Phase 2 completion proof: at-rest encryption + network directory.

Run: python -m pytest test_agent_phase2.py -q
"""
import os
import json
import shutil
import tempfile

import pytest

from decisiongraph.agent_access import AgentAccessStore, AgentAccessError
from decisiongraph.agent_crypto import FileEncryptor, load_or_create_key
from decisiongraph.agent_network import network_directory


@pytest.fixture()
def base():
    d = tempfile.mkdtemp(prefix="dg_base_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ── at-rest encryption ───────────────────────────────────────────────────
def test_encrypted_grants_file_is_not_plaintext(base):
    keyp = os.path.join(base, "k.key")
    ws = os.path.join(base, "wsA")
    s = AgentAccessStore(ws, encrypt=True, key_path=keyp)
    g = s.mint("secret-bot", ["pricing"])
    raw = open(s._grants_path, "rb").read()
    # the agent name / token must NOT appear in cleartext on disk
    assert b"secret-bot" not in raw
    assert g["token"].encode() not in raw
    # but the store still reads it back correctly
    assert s.get(g["token"])["agent_name"] == "secret-bot"


def test_encrypted_audit_lines_are_ciphertext_but_readable(base):
    keyp = os.path.join(base, "k.key")
    ws = os.path.join(base, "wsB")
    s = AgentAccessStore(ws, encrypt=True, key_path=keyp)
    g = s.mint("bot", ["pricing"])
    s.authorize(g["token"], "pricing")
    disk = open(s._audit_path, "r", encoding="utf-8").read()
    assert "access_granted" not in disk          # encrypted on disk
    assert "grant_minted" not in disk
    rows = s.read_audit()                          # decrypts transparently
    assert [r["event"] for r in rows] == ["grant_minted", "access_granted"]


def test_wrong_key_cannot_read(base):
    ws = os.path.join(base, "wsC")
    k1 = os.path.join(base, "k1.key")
    s1 = AgentAccessStore(ws, encrypt=True, key_path=k1)
    g = s1.mint("bot", ["pricing"])
    # a different key file => different key => cannot decrypt grants
    k2 = os.path.join(base, "k2.key")
    s2 = AgentAccessStore(ws, encrypt=True, key_path=k2)
    assert s2.get(g["token"]) is None             # fails closed, no crash


def test_plaintext_default_still_works_and_is_readable(base):
    ws = os.path.join(base, "wsD")
    s = AgentAccessStore(ws)                       # default: NOT encrypted
    g = s.mint("bot", ["pricing"])
    raw = open(s._grants_path, "rb").read()
    assert b"bot" in raw                           # plaintext as before
    assert s.get(g["token"])["agent_name"] == "bot"


def test_key_is_stable_across_instances(base):
    keyp = os.path.join(base, "shared.key")
    k1 = load_or_create_key(keyp)
    k2 = load_or_create_key(keyp)
    assert k1 == k2                                # same key file => same key
    enc = FileEncryptor(key_path=keyp)
    assert enc.decrypt_text(enc.encrypt_text("hi")) == "hi"


# ── network directory (multi-company, anonymized) ────────────────────────
def _seed_company(base, name, agent, topic, *, deny=False):
    ws = os.path.join(base, name)
    s = AgentAccessStore(ws)
    g = s.mint(agent, [topic])
    if deny:
        try:
            s.authorize(g["token"], "OUT_OF_SCOPE")
        except AgentAccessError:
            pass
    else:
        s.authorize(g["token"], topic)
        s.record_event("job_completed", agent_name=agent, learning_id="L1")
    return s


def test_network_directory_aggregates_multiple_companies(base):
    _seed_company(base, "wsX", "ax", "pricing")
    _seed_company(base, "wsY", "ay", "refunds")
    _seed_company(base, "wsZ", "az", "pricing", deny=True)
    d = network_directory(base)
    assert d["totals"]["companies"] == 3
    assert d["totals"]["jobs_completed"] == 2
    assert d["totals"]["scope_denials"] == 1


def test_network_directory_is_anonymized(base):
    _seed_company(base, "ws_secret_token", "confidential-agent", "pricing")
    d = network_directory(base)
    blob = json.dumps(d)
    assert "confidential-agent" not in blob        # no agent names
    assert "ws_secret_token" not in blob           # no raw workspace tokens
    assert d["companies"][0]["company"].startswith("co_")  # opaque id only


def test_network_skips_workspaces_without_agents(base):
    os.makedirs(os.path.join(base, "empty_ws"), exist_ok=True)
    _seed_company(base, "real_ws", "a", "pricing")
    d = network_directory(base)
    assert d["totals"]["companies"] == 1
