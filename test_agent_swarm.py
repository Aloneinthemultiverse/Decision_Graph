"""Swarm (parallel multi-agent) + run_python sandbox tool tests.

Run: python -m pytest test_agent_swarm.py -q
"""
import os
import time
import json
import shutil
import tempfile

import pytest

from decisiongraph.agent_access import AgentAccessStore
from decisiongraph.orchestration import run_swarm
from decisiongraph.mcp_tools import _h_run_python
from decisiongraph.agent_sandbox import docker_available


@pytest.fixture()
def ws_root():
    d = tempfile.mkdtemp(prefix="dg_ws_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class FakeAdapter:
    def __init__(self):
        self.learnings = []
    def fetch_scope(self, topic):
        return [{"kind": "decision", "question": f"{topic}?",
                 "answer": f"{topic} answer", "confidence": 1.0}]
    def store_learning(self, question, answer, reasoning):
        lid = f"L{len(self.learnings)}"; self.learnings.append({"q": question, "a": answer}); return lid


# ── swarm logic (no docker required) ──────────────────────────────────────
def test_swarm_runs_members_in_parallel(ws_root, monkeypatch):
    s = AgentAccessStore(ws_root)
    from decisiongraph import orchestration as orch

    # 3 fake agents
    monkeypatch.setattr(orch, "get_agent",
        lambda a: {"id": a, "name": f"bot-{a}", "script_path": "/dev/null", "tags": []})

    # each job sleeps 0.5s — if run in parallel total ~0.5s, if sequential ~1.5s
    def slow(**kw):
        time.sleep(0.5)
        return {"ok": True, "result": {"answer": f"hi from {kw['topics'][0]}",
                                        "citation": "test"},
                "learning_id": "L", "duration_s": 0.5, "via": "staged",
                "scope": kw["topics"]}
    monkeypatch.setattr(orch, "run_agent_job", slow)
    monkeypatch.setattr(orch, "run_agent_job_mcp", slow)
    monkeypatch.setattr(orch, "run_agent_job_react", slow)

    members = [
        {"agent_id": "a1", "topics": ["t1"], "task": "x"},
        {"agent_id": "a2", "topics": ["t2"], "task": "x"},
        {"agent_id": "a3", "topics": ["t3"], "task": "x"},
    ]
    t0 = time.time()
    out = run_swarm(s, FakeAdapter(), object(), members, goal="test")
    dur = time.time() - t0

    assert out["status"] == "ok"
    assert out["members_count"] == 3
    assert all(m["ok"] for m in out["members"])
    # parallel: 3 × 0.5s sequential would be 1.5s; parallel should be ~0.5-0.9s
    assert dur < 1.2, f"swarm appears to be running sequentially (took {dur:.2f}s)"


def test_swarm_records_run_history(ws_root, monkeypatch):
    s = AgentAccessStore(ws_root)
    from decisiongraph import orchestration as orch
    monkeypatch.setattr(orch, "get_agent",
        lambda a: {"id": a, "name": a, "script_path": "/x", "tags": []})
    monkeypatch.setattr(orch, "run_agent_job",
        lambda **kw: {"ok": True, "result": {"answer": "y", "citation": "z"},
                      "learning_id": "L", "duration_s": 0.01, "via": "staged"})
    out = run_swarm(s, FakeAdapter(), object(),
                    [{"agent_id": "a", "topics": ["t"], "task": "x"}],
                    goal="g")
    runs = orch.list_runs(s)
    assert any(r["run_id"] == out["run_id"] for r in runs)


def test_swarm_partial_on_member_failure(ws_root, monkeypatch):
    s = AgentAccessStore(ws_root)
    from decisiongraph import orchestration as orch
    from decisiongraph.agent_job import AgentJobError

    monkeypatch.setattr(orch, "get_agent",
        lambda a: {"id": a, "name": a, "script_path": "/x", "tags": []})

    calls = {"i": 0}
    def maybe_fail(**kw):
        calls["i"] += 1
        if calls["i"] == 2:
            raise AgentJobError("simulated swarm member failure")
        return {"ok": True, "result": {"answer": "y", "citation": "z"},
                "learning_id": "L", "duration_s": 0.01, "via": "staged"}
    monkeypatch.setattr(orch, "run_agent_job", maybe_fail)

    out = run_swarm(s, FakeAdapter(), object(), [
        {"agent_id": "a", "topics": ["t"], "task": "x"},
        {"agent_id": "b", "topics": ["t"], "task": "x"},
        {"agent_id": "c", "topics": ["t"], "task": "x"},
    ], goal="")
    assert out["status"] == "partial"
    assert sum(1 for m in out["members"] if m["ok"]) == 2
    assert sum(1 for m in out["members"] if not m["ok"]) == 1


def test_unknown_agent_id_aborts(ws_root, monkeypatch):
    s = AgentAccessStore(ws_root)
    from decisiongraph import orchestration as orch
    from decisiongraph.agent_catalog import CatalogError
    def boom(a): raise CatalogError(f"unknown {a}")
    monkeypatch.setattr(orch, "get_agent", boom)
    out = run_swarm(s, FakeAdapter(), object(),
                    [{"agent_id": "nope", "topics": ["t"], "task": "x"}], goal="")
    assert out["status"] == "failed"
    assert "unknown" in (out.get("error") or "")


# ── run_python tool (real docker) ────────────────────────────────────────
@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_run_python_returns_stdout():
    out = _h_run_python(None, {"code": "print(2+2)"}, {"JOBS": None}, owner=True)
    assert out["ok"] is True
    assert out["stdout"].strip() == "4"


@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_run_python_blocks_network():
    code = ("import socket\n"
            "ok=False\n"
            "try:\n"
            "  socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
            "  ok=True\n"
            "except Exception: pass\n"
            "print('NET_OK' if ok else 'NET_BLOCKED')\n")
    out = _h_run_python(None, {"code": code, "timeout_seconds": 10},
                       {"JOBS": None}, owner=True)
    assert out["ok"] is True
    assert "NET_BLOCKED" in out["stdout"]


@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_run_python_enforces_timeout():
    out = _h_run_python(None, {"code": "import time;time.sleep(30)",
                                "timeout_seconds": 2},
                       {"JOBS": None}, owner=True)
    assert "error" in out
    assert "timeout" in out["error"].lower()


@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_run_python_captures_nonzero_exit():
    out = _h_run_python(None, {"code": "import sys;sys.exit(7)"},
                       {"JOBS": None}, owner=True)
    assert out["ok"] is False
    assert out["exit_code"] == 7
