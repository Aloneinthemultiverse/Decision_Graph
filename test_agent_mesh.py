"""S5 tool-mesh tests: declare/list/call peer tools through the bus.

Run: python -m pytest test_agent_mesh.py -q
"""
import os
import time
import json
import shutil
import tempfile
import threading

import pytest

from decisiongraph.agent_access import AgentAccessStore
from decisiongraph.agent_messages import MessageBus, get_bus
from decisiongraph.mcp_gateway import MCPGateway


@pytest.fixture()
def ws_root():
    d = tempfile.mkdtemp(prefix="dg_ws_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _gateway(store, ws_root, agent, run_id):
    g = store.mint(agent, ["x"])
    bus = get_bus(ws_root)
    return MCPGateway(store=store, adapter=None, agent_token=g["token"],
                      job_id=f"j_{agent}", ws=object(),
                      ctx={"bus": bus, "run_id": run_id,
                           "agent_name": agent})


def _call(gw, name, args, _id=[100]):
    _id[0] += 1
    r = gw.handle({"jsonrpc": "2.0", "id": _id[0],
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args}})
    if "error" in r:
        return {"_err": r["error"]}
    return json.loads(r["result"]["content"][0]["text"])


def test_declare_and_list_peer_tools(ws_root):
    store = AgentAccessStore(ws_root)
    alice = _gateway(store, ws_root, "alice", "rT")
    _call(alice, "register_peer_tool", {
        "name": "calculate",
        "description": "math",
        "input_schema": {"type": "object", "properties": {}}})
    # bob lists what's available
    bob = _gateway(store, ws_root, "bob", "rT")
    payload = _call(bob, "list_peer_tools", {})
    assert payload["count"] == 1
    assert payload["tools"][0]["provider"] == "alice"
    assert payload["tools"][0]["name"] == "calculate"


def test_own_tools_filtered_from_list(ws_root):
    store = AgentAccessStore(ws_root)
    alice = _gateway(store, ws_root, "alice", "rT")
    _call(alice, "register_peer_tool",
          {"name": "calculate", "description": "math"})
    # alice listing peer tools should NOT see her own
    payload = _call(alice, "list_peer_tools", {})
    assert payload["count"] == 0


def test_call_peer_tool_full_roundtrip(ws_root):
    """Stand up a tiny 'mesh provider' thread that listens for tool_request
    messages and posts tool_response back. Verify call_peer_tool blocks,
    receives the response, and returns the result."""
    store = AgentAccessStore(ws_root)
    bus = get_bus(ws_root)
    run_id = "rT"
    alice = _gateway(store, ws_root, "alice", run_id)
    # alice declares she provides "add"
    _call(alice, "register_peer_tool", {"name": "add", "description": "sum"})

    stop = threading.Event()

    def provider():
        since_ts = 0.0
        while not stop.is_set():
            msgs = bus.wait_for(run_id, "alice", since_ts=since_ts, timeout_s=1)
            for m in msgs:
                since_ts = max(since_ts, m.get("ts", 0))
                if m.get("kind") != "tool_request":
                    continue
                body = json.loads(m["text"])
                args = body.get("args") or {}
                if body.get("tool_name") == "add":
                    result = {"sum": args.get("a", 0) + args.get("b", 0)}
                else:
                    result = {"error": "unknown"}
                bus.post(run_id, "alice", m["from"],
                          json.dumps({"req_id": body["req_id"],
                                       "result": result}),
                          kind="tool_response")

    t = threading.Thread(target=provider, daemon=True); t.start()
    try:
        bob = _gateway(store, ws_root, "bob", run_id)
        r = _call(bob, "call_peer_tool",
                  {"provider": "alice", "tool_name": "add",
                   "args": {"a": 3, "b": 4},
                   "timeout_seconds": 8})
        assert r.get("ok") is True
        assert r["from"] == "alice"
        assert r["result"]["sum"] == 7
    finally:
        stop.set()


def test_call_peer_tool_timeout(ws_root):
    store = AgentAccessStore(ws_root)
    bob = _gateway(store, ws_root, "bob", "rT")
    r = _call(bob, "call_peer_tool",
              {"provider": "nobody-here", "tool_name": "anything",
               "args": {}, "timeout_seconds": 2})
    assert r.get("_err"), r
    assert "timed out" in (r["_err"].get("message") or "").lower()


def test_mesh_tools_refuse_without_run(ws_root):
    store = AgentAccessStore(ws_root)
    g = store.mint("alice", ["x"])
    gw = MCPGateway(store=store, adapter=None, agent_token=g["token"],
                    job_id="j", ws=object(), ctx={})    # no bus/run_id
    r = gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "register_peer_tool",
                              "arguments": {"name": "x"}}})
    assert "error" in r


def test_kind_routes_correctly(ws_root):
    """post_message with kind=tool_request should be visible as such to peers."""
    store = AgentAccessStore(ws_root)
    alice = _gateway(store, ws_root, "alice", "rT")
    _call(alice, "post_message",
          {"to": "bob", "text": "ping", "kind": "tool_request"})
    bob = _gateway(store, ws_root, "bob", "rT")
    msgs = _call(bob, "read_messages", {})["messages"]
    assert len(msgs) == 1 and msgs[0]["kind"] == "tool_request"
