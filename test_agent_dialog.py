"""S3+S4 tests: message bus + dialog orchestration logic.

Run: python -m pytest test_agent_dialog.py -q
"""
import os
import time
import json
import shutil
import tempfile
import threading

import pytest

from decisiongraph.agent_messages import MessageBus, get_bus
from decisiongraph.agent_access import AgentAccessStore


@pytest.fixture()
def ws_root():
    d = tempfile.mkdtemp(prefix="dg_ws_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ── MessageBus ──────────────────────────────────────────────────────────
def test_post_and_read(ws_root):
    b = MessageBus(ws_root)
    r = b.post("run1", "alice", "all", "hello world")
    assert r["id"]
    msgs = b.read("run1", "bob")  # bob reads broadcast
    assert len(msgs) == 1 and msgs[0]["from"] == "alice"


def test_no_self_echo(ws_root):
    b = MessageBus(ws_root)
    b.post("run1", "alice", "all", "hi")
    assert b.read("run1", "alice") == []        # alice doesn't see her own msg


def test_targeted_message(ws_root):
    b = MessageBus(ws_root)
    b.post("run1", "alice", "bob", "psst")
    assert len(b.read("run1", "bob")) == 1      # bob sees it
    assert b.read("run1", "carol") == []        # carol doesn't


def test_isolation_between_runs(ws_root):
    b = MessageBus(ws_root)
    b.post("runA", "alice", "all", "A")
    b.post("runB", "alice", "all", "B")
    rA = b.read("runA", "bob"); rB = b.read("runB", "bob")
    assert len(rA) == 1 and rA[0]["text"] == "A"
    assert len(rB) == 1 and rB[0]["text"] == "B"


def test_peers(ws_root):
    b = MessageBus(ws_root)
    b.post("r", "alice", "all", "1"); b.post("r", "bob", "all", "2")
    b.post("r", "alice", "all", "3")
    assert sorted(b.peers("r")) == ["alice", "bob"]


def test_wait_for_blocks_then_returns(ws_root):
    b = MessageBus(ws_root)

    def _producer():
        time.sleep(0.4)
        b.post("r", "alice", "all", "delayed hello")

    threading.Thread(target=_producer, daemon=True).start()
    t0 = time.time()
    msgs = b.wait_for("r", "bob", since_ts=0, timeout_s=5)
    dur = time.time() - t0
    assert len(msgs) == 1
    assert 0.3 < dur < 1.5                       # woke up close to producer time


def test_wait_for_timeout(ws_root):
    b = MessageBus(ws_root)
    t0 = time.time()
    msgs = b.wait_for("r", "bob", since_ts=0, timeout_s=1)
    assert msgs == []
    assert time.time() - t0 >= 0.9              # waited the full timeout


def test_persistence_across_instances(ws_root):
    b1 = MessageBus(ws_root)
    b1.post("r", "alice", "all", "hi")
    b2 = MessageBus(ws_root)                    # re-open
    assert len(b2.read("r", "bob")) == 1


def test_singleton_per_workspace(ws_root):
    a = get_bus(ws_root); b = get_bus(ws_root)
    assert a is b


# ── MCP tools dispatching the bus ───────────────────────────────────────
def test_mcp_send_and_read_via_gateway(ws_root):
    """Use the actual MCPGateway dispatcher with the bus in ctx."""
    from decisiongraph.mcp_gateway import MCPGateway
    store = AgentAccessStore(ws_root)
    g = store.mint("alice", ["x"])
    bus = get_bus(ws_root)
    ctx = {"bus": bus, "run_id": "rTest", "agent_name": "alice"}
    gw = MCPGateway(store=store, adapter=None, agent_token=g["token"],
                    job_id="j1", ws=object(), ctx=ctx)
    # send
    r = gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "post_message",
                              "arguments": {"to": "all", "text": "ahoy"}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["ok"] is True
    # bob reads
    g2 = store.mint("bob", ["x"])
    gw2 = MCPGateway(store=store, adapter=None, agent_token=g2["token"],
                     job_id="j2", ws=object(),
                     ctx={"bus": bus, "run_id": "rTest", "agent_name": "bob"})
    r2 = gw2.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "read_messages", "arguments": {}}})
    p2 = json.loads(r2["result"]["content"][0]["text"])
    assert p2["count"] == 1 and p2["messages"][0]["text"] == "ahoy"


def test_mcp_message_tools_refuse_without_run(ws_root):
    """Outside a dialog run (no bus/run_id in ctx), the tools error cleanly."""
    from decisiongraph.mcp_gateway import MCPGateway
    store = AgentAccessStore(ws_root)
    g = store.mint("alice", ["x"])
    gw = MCPGateway(store=store, adapter=None, agent_token=g["token"],
                    job_id="j", ws=object(), ctx={})    # no bus/run_id
    r = gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "post_message",
                              "arguments": {"text": "x"}}})
    assert "error" in r


def test_dialog_writes_final_positions_to_dg_memory(ws_root, monkeypatch):
    """Regression — every dialog member's final synthesis must land in the
    company DG memory as a [dialog:<role>] decision, not just the transcript."""
    from decisiongraph.agent_access import AgentAccessStore
    from decisiongraph import orchestration as orch

    store = AgentAccessStore(ws_root)

    # Fake DG memory we can inspect
    class _FakeMem:
        def __init__(self): self.stored = []
        def store(self, **kw):
            did = f"D{len(self.stored)}"
            self.stored.append({"id": did, **kw}); return did
        def save(self): pass

    class _FakeDG:
        def __init__(self): self.memory = _FakeMem()

    fake_dg = _FakeDG()

    # Stub get_agent + run_sandboxed_mcp so we don't need docker
    monkeypatch.setattr(orch, "get_agent",
        lambda aid: {"id": aid, "name": aid, "script_path": "/dev/null", "tags": []})

    def fake_run_sandboxed_mcp(agent_script, gateway, task_payload,
                                cfg=None, max_calls=128):
        # Simulate the agent finishing with a final answer
        gateway.final_answer = {
            "answer": f"Final position from {task_payload['agent_name']}.",
            "citation": f"dialog/{task_payload['run_id']}"}
        gateway.call_count = 5
        return {"duration_s": 0.1}

    monkeypatch.setattr(orch, "run_sandboxed_mcp", None, raising=False)
    import decisiongraph.agent_sandbox as sb
    monkeypatch.setattr(sb, "run_sandboxed_mcp", fake_run_sandboxed_mcp)

    members = [
        {"agent_id": "a", "agent_role": "Believer", "topics": ["x"], "task": "argue yes"},
        {"agent_id": "a", "agent_role": "Skeptic",  "topics": ["x"], "task": "argue no"},
    ]
    summary = orch.run_dialog(store, adapter=None, ws_dg=fake_dg,
                              members=members, goal="test goal",
                              max_rounds=1)
    assert summary["status"] == "ok"
    assert len(summary["learnings_written"]) == 2
    # Verify each stored decision is tagged with the dialog role
    questions = [d["question"] for d in fake_dg.memory.stored]
    assert any("[dialog:Believer]" in q for q in questions), questions
    assert any("[dialog:Skeptic]"  in q for q in questions), questions


def test_list_peers(ws_root):
    from decisiongraph.mcp_gateway import MCPGateway
    store = AgentAccessStore(ws_root)
    bus = get_bus(ws_root)
    bus.post("rZ", "alice", "all", "1"); bus.post("rZ", "bob", "all", "2")
    g = store.mint("carol", ["x"])
    gw = MCPGateway(store=store, adapter=None, agent_token=g["token"],
                    job_id="j", ws=object(),
                    ctx={"bus": bus, "run_id": "rZ", "agent_name": "carol"})
    r = gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "list_peers", "arguments": {}}})
    p = json.loads(r["result"]["content"][0]["text"])
    assert sorted(p["peers"]) == ["alice", "bob"]
    assert p["self"] == "carol"
