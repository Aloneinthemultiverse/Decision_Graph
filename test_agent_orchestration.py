"""Pipeline orchestration tests — purely logic, no docker required.

Run: python -m pytest test_agent_orchestration.py -q
"""
import os
import json
import shutil
import tempfile

import pytest

from decisiongraph.agent_access import AgentAccessStore
from decisiongraph.orchestration import run_pipeline, list_runs, get_run


@pytest.fixture()
def ws_root():
    d = tempfile.mkdtemp(prefix="dg_ws_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class FakeAdapter:
    def __init__(self):
        self.learnings = []

    def fetch_scope(self, topic):
        # one fake decision per topic so the staged knowledge_qa agent has data
        return [{"kind": "decision", "question": f"{topic}?",
                 "answer": f"{topic} answer", "confidence": 1.0}]

    def store_learning(self, question, answer, reasoning):
        lid = f"L{len(self.learnings)}"
        self.learnings.append({"q": question, "a": answer})
        return lid


def test_pipeline_two_stages_ok(ws_root, monkeypatch):
    s = AgentAccessStore(ws_root)
    ad = FakeAdapter()

    # bypass real catalog + runner: monkeypatch run_agent_job and get_agent
    from decisiongraph import orchestration as orch

    def fake_get_agent(agent_id):
        return {"id": agent_id, "name": f"agent-{agent_id}",
                "script_path": "/dev/null", "tags": []}
    def fake_runner(store, adapter, agent_token, topics, agent_script, task):
        # mimic a real successful job
        store.record_event("job_completed", token=agent_token,
                           agent_name=f"agent-{topics[0]}",
                           learning_id="L0")
        return {"job_id": "fake", "agent_name": f"agent-{topics[0]}",
                "ok": True, "result": {"answer": f"answer-for-{topics[0]}",
                                       "citation": "test"},
                "learning_id": "L0", "duration_s": 0.01, "token_revoked": True,
                "scope": topics}
    monkeypatch.setattr(orch, "get_agent", fake_get_agent)
    monkeypatch.setattr(orch, "run_agent_job", fake_runner)
    monkeypatch.setattr(orch, "run_agent_job_mcp", fake_runner)

    out = run_pipeline(s, ad, [
        {"agent_id": "a1", "topics": ["pricing"], "task": "step 1"},
        {"agent_id": "a2", "topics": ["refunds"], "task": "step 2"},
    ])
    assert out["status"] == "ok"
    assert out["stages_count"] == 2
    assert all(st["ok"] for st in out["stages"])
    # second stage's task should have prior-stage context prepended
    # (we don't expose the modified task back, but we trust pass_through)
    assert "answer-for-refunds" in out["final_answer_preview"]


def test_pipeline_records_run_history(ws_root, monkeypatch):
    s = AgentAccessStore(ws_root)
    from decisiongraph import orchestration as orch
    monkeypatch.setattr(orch, "get_agent",
        lambda a: {"id": a, "name": a, "script_path": "/x", "tags": []})
    monkeypatch.setattr(orch, "run_agent_job",
        lambda **kw: {"ok": True, "result": {"answer": "y", "citation": "z"},
                      "learning_id": "L", "duration_s": 0.1})
    monkeypatch.setattr(orch, "run_agent_job_mcp",
        lambda **kw: {"ok": True, "result": {"answer": "y", "citation": "z"},
                      "learning_id": "L", "duration_s": 0.1})
    run_pipeline(s, FakeAdapter(),
                 [{"agent_id": "a", "topics": ["t"], "task": "x"}])
    runs = list_runs(s)
    assert len(runs) == 1
    assert get_run(s, runs[0]["run_id"]) is not None


def test_pipeline_failure_stops_chain(ws_root, monkeypatch):
    s = AgentAccessStore(ws_root)
    from decisiongraph import orchestration as orch
    from decisiongraph.agent_job import AgentJobError

    monkeypatch.setattr(orch, "get_agent",
        lambda a: {"id": a, "name": a, "script_path": "/x", "tags": []})

    def boom(**kw):
        raise AgentJobError("simulated failure")
    monkeypatch.setattr(orch, "run_agent_job", boom)
    monkeypatch.setattr(orch, "run_agent_job_mcp", boom)

    out = run_pipeline(s, FakeAdapter(), [
        {"agent_id": "a1", "topics": ["t1"], "task": "1"},
        {"agent_id": "a2", "topics": ["t2"], "task": "2"},
    ])
    assert out["status"] == "failed"
    assert len(out["stages"]) == 1   # second stage never ran
    assert "orchestration_failed" in [r["event"] for r in s.read_audit()]
