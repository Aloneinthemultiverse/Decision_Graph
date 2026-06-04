"""MCP-in-sandbox path tests: gateway enforces scope on every call, audit
records each MCP call, the MCP-speaking agent reaches the graph correctly.

Run: python -m pytest test_agent_mcp.py -q
"""
import os
import json
import shutil
import tempfile

import pytest

from decisiongraph.agent_access import AgentAccessStore
from decisiongraph.agent_job import run_agent_job_mcp, AgentJobError
from decisiongraph.agent_sandbox import docker_available
from decisiongraph.mcp_gateway import MCPGateway


@pytest.fixture()
def ws_root():
    d = tempfile.mkdtemp(prefix="dg_ws_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class FakeAdapter:
    """Returns scoped evidence resembling decisions + triples."""
    DATA = {
        "attention": [
            {"kind": "triple", "subject": "Transformer",
             "relation": "relies on", "object": "attention mechanism"},
            {"kind": "triple", "subject": "Transformer",
             "relation": "based on", "object": "self-attention"},
        ],
        "pricing": [
            {"kind": "decision", "question": "Pricing?",
             "answer": "Tiered Free/Pro/Ent", "confidence": 1.0}],
    }

    def __init__(self):
        self.learnings = []

    def fetch_scope(self, topic):
        return list(self.DATA.get(topic.lower(), []))

    def store_learning(self, question, answer, reasoning):
        lid = f"L{len(self.learnings)}"
        self.learnings.append({"q": question, "a": answer})
        return lid


# ── gateway: pure unit tests (no docker) ─────────────────────────────────
def test_gateway_initialize_and_tools_list(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["attention"])
    gw = MCPGateway(s, FakeAdapter(), g["token"], "job1")
    init = gw.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["protocolVersion"]
    tl = gw.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in tl["result"]["tools"]}
    assert {"recall", "scoped_search", "done"} <= names


def test_gateway_recall_in_scope(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["attention"])
    gw = MCPGateway(s, FakeAdapter(), g["token"], "job1")
    r = gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "recall",
                              "arguments": {"topic": "attention"}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["count"] == 2
    # audit recorded the call
    ev = [r["event"] for r in s.read_audit()]
    assert "mcp_call" in ev


def test_gateway_denies_out_of_scope(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["attention"])
    gw = MCPGateway(s, FakeAdapter(), g["token"], "job1")
    r = gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "recall",
                              "arguments": {"topic": "salaries"}}})
    assert "error" in r and "out of scope" in r["error"]["message"]
    ev = [r["event"] for r in s.read_audit()]
    assert "access_denied" in ev


def test_gateway_requires_topic_argument(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["attention"])
    gw = MCPGateway(s, FakeAdapter(), g["token"], "job1")
    r = gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "recall", "arguments": {}}})
    assert "error" in r
    ev = [r["event"] for r in s.read_audit()]
    assert "mcp_denied" in ev


def test_gateway_scoped_search_ranks(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["attention"])
    gw = MCPGateway(s, FakeAdapter(), g["token"], "job1")
    r = gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "scoped_search",
                              "arguments": {"topic": "attention",
                                            "query": "transformer mechanism"}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["hits"] and payload["hits"][0]["kind"] == "triple"


def test_gateway_done_captures_final_answer(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["attention"])
    gw = MCPGateway(s, FakeAdapter(), g["token"], "job1")
    gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "done",
                          "arguments": {"answer": "X relies on Y",
                                        "citation": "triple"}}})
    assert gw.final_answer == {"answer": "X relies on Y",
                               "citation": "triple"}


# ── full path (real docker) ──────────────────────────────────────────────
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_mcp_full_lifecycle_in_real_sandbox(ws_root):
    s = AgentAccessStore(ws_root)
    ad = FakeAdapter()
    g = s.mint("kqa-mcp", ["attention"])
    script = os.path.join(os.path.dirname(__file__),
                          "agentnet", "agents", "knowledge_qa_mcp.py")
    out = run_agent_job_mcp(s, ad, g["token"], ["attention"], script,
                            "What does Transformer rely on?")
    assert out["ok"] is True
    assert out["via"] == "mcp"
    assert out["mcp_calls"] >= 1
    # the agent's answer should mention "attention" (from the in-scope triples)
    assert "attention" in out["result"]["answer"].lower()
    assert len(ad.learnings) == 1
    assert s.get(g["token"])["revoked"] is True
    # audit shows MCP-flavored events
    events = [r["event"] for r in s.read_audit()]
    assert "mcp_call" in events
    assert "mcp_done" in events
    assert "job_completed" in events
