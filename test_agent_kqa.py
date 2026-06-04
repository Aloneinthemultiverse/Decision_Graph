"""Knowledge-QA agent + graph-aware adapter tests.

Run: python -m pytest test_agent_kqa.py -q
"""
import os
import shutil
import tempfile
import subprocess
import json
import textwrap

import pytest

from decisiongraph.agent_job import WorkspaceMemoryAdapter
from decisiongraph.agent_sandbox import (
    run_sandboxed, docker_available, SandboxConfig)


# ── fake DG that exposes the surface the adapter reads ────────────────────
class FakeDG:
    """Minimal stand-in for a DecisionGraph instance — exposes .memory,
    .G (NetworkX-like), and .summaries."""
    class _Memory:
        def __init__(self, decisions): self._d = decisions
        def get_active_decisions(self): return self._d

    class _Graph:
        def __init__(self, nodes, edges):
            self._nodes = list(nodes)
            self._edges = list(edges)  # (u, v, {"relation": ...})
        def nodes(self): return self._nodes
        def edges(self, data=False):
            return self._edges if data else [(u, v) for u, v, _ in self._edges]

    def __init__(self, decisions=(), nodes=(), edges=(), summaries=None):
        self.memory = FakeDG._Memory(list(decisions))
        self.G = FakeDG._Graph(nodes, edges) if nodes or edges else None
        self.summaries = summaries or {}


def test_adapter_returns_decisions_for_topic():
    dg = FakeDG(decisions=[
        {"question": "Pricing model?", "answer": "Tiered Free/Pro",
         "confidence": 0.9, "timestamp": "t"},
        {"question": "Refund policy?", "answer": "30 days", "confidence": 0.8},
    ])
    a = WorkspaceMemoryAdapter(dg)
    out = a.fetch_scope("pricing")
    assert len(out) == 1 and out[0]["kind"] == "decision"


def test_adapter_returns_graph_triples_for_topic():
    # mimic PDF-ingest output: nodes + relation edges
    nodes = ["Acme Corp", "pricing tier Pro", "$29/month"]
    edges = [("Acme Corp", "pricing tier Pro", {"relation": "offers"}),
             ("pricing tier Pro", "$29/month", {"relation": "costs"})]
    dg = FakeDG(nodes=nodes, edges=edges)
    a = WorkspaceMemoryAdapter(dg)
    out = a.fetch_scope("pricing")
    kinds = [e["kind"] for e in out]
    assert "triple" in kinds
    triples = [e for e in out if e["kind"] == "triple"]
    assert any(e["object"] == "$29/month" for e in triples)


def test_adapter_returns_community_summaries():
    dg = FakeDG(summaries={1: {"summary": "Pricing is tiered: Free/Pro/Ent"},
                            2: {"summary": "Hiring uses paid trial weeks"}})
    a = WorkspaceMemoryAdapter(dg)
    out = a.fetch_scope("pricing")
    assert any(e["kind"] == "summary" and "Pricing" in e["summary"]
               for e in out)


def test_adapter_off_scope_returns_nothing():
    dg = FakeDG(
        decisions=[{"question": "Pricing?", "answer": "Tiered",
                    "confidence": 1}],
        nodes=["Pricing"],
        edges=[("Pricing", "Tiered", {"relation": "is"})],
        summaries={1: {"summary": "Pricing notes"}})
    a = WorkspaceMemoryAdapter(dg)
    assert a.fetch_scope("salaries") == []


# ── the QA agent itself (no docker needed: run the script directly) ─────────
def _run_agent_inline(scoped_input: dict) -> dict:
    """Run the QA agent as a normal python script with input mocked via stdin
    redirection — independent of Docker (the sandbox is tested elsewhere)."""
    import json as J
    work = tempfile.mkdtemp(prefix="kqa_")
    try:
        # the agent reads /sandbox/input.json — create a local /sandbox dir
        # is not portable on Windows. Use a small shim instead.
        script = os.path.join(os.path.dirname(__file__),
                              "agentnet", "agents", "knowledge_qa.py")
        src = open(script, "r", encoding="utf-8").read()
        # patch the input path to a temp file
        inp = os.path.join(work, "input.json")
        with open(inp, "w", encoding="utf-8") as f:
            J.dump(scoped_input, f)
        shimmed = src.replace('"/sandbox/input.json"', J.dumps(inp))
        shim = os.path.join(work, "agent_shim.py")
        with open(shim, "w", encoding="utf-8") as f:
            f.write(shimmed)
        r = subprocess.run(["python", shim], capture_output=True, text=True,
                           timeout=15)
        assert r.returncode == 0, r.stderr
        return J.loads(r.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_qa_agent_picks_best_decision():
    out = _run_agent_inline({
        "task": "What is our pricing?",
        "data": {"pricing": [
            {"kind": "decision", "question": "Refund?", "answer": "30 days",
             "confidence": 1.0},
            {"kind": "decision", "question": "Pricing model?",
             "answer": "Tiered: Free, Pro $29, Enterprise.",
             "confidence": 0.95},
        ]}})
    assert "Tiered" in out["answer"]
    assert out["citation"].startswith("decision")
    assert out["scope_seen"]["by_kind"]["decision"] == 2


def test_qa_agent_uses_graph_triples_when_no_decisions():
    out = _run_agent_inline({
        "task": "What does Acme charge for Pro?",
        "data": {"pricing": [
            {"kind": "triple", "subject": "Acme Pro plan",
             "relation": "costs", "object": "$29 per month"},
            {"kind": "triple", "subject": "Acme Free plan",
             "relation": "costs", "object": "$0"},
        ]}})
    assert "29" in out["answer"] or "Acme" in out["answer"]
    assert out["citation"] == "knowledge graph triple"


def test_qa_agent_handles_empty_scope():
    out = _run_agent_inline({"task": "anything?", "data": {"pricing": []}})
    assert "No evidence" in out["answer"]
    assert out["citation"] == "n/a"


@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_qa_agent_runs_in_real_sandbox():
    """Belt + braces: agent runs in the actual network-less Docker sandbox."""
    script = os.path.join(os.path.dirname(__file__),
                          "agentnet", "agents", "knowledge_qa.py")
    out = run_sandboxed(script, {
        "task": "pricing tier?",
        "data": {"pricing": [
            {"kind": "decision", "question": "Pricing tiers?",
             "answer": "Free/Pro/Enterprise", "confidence": 1.0}]}},
        SandboxConfig(timeout_s=30))
    assert out["ok"] is True
    assert out["result"]["citation"].startswith("decision")
