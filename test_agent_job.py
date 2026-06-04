"""Slice-1 Step 4 proof: the full audited lifecycle.

Logic tests use a fake memory adapter + fake runner (no Docker / no embed
model). One integration test runs the real bundled agent in the real
sandbox; it SKIPS if Docker is down.

Run: python -m pytest test_agent_job.py -q
"""
import os
import shutil
import tempfile

import pytest

from decisiongraph.agent_access import AgentAccessStore
from decisiongraph.agent_job import run_agent_job, AgentJobError
from decisiongraph.agent_sandbox import docker_available


@pytest.fixture()
def ws_root():
    d = tempfile.mkdtemp(prefix="dg_ws_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class FakeAdapter:
    def __init__(self):
        self.learnings = []
        self.data = {"pricing": [{"question": "price?", "answer": "Tiered",
                                  "confidence": 0.9, "timestamp": "t"}]}

    def fetch_scope(self, topic):
        return self.data.get(topic.lower(), [])

    def store_learning(self, question, answer, reasoning):
        lid = f"L{len(self.learnings)}"
        self.learnings.append({"id": lid, "q": question, "a": answer})
        return lid


def ok_runner(script, scoped_input, cfg):
    return {"ok": True, "result": {"answer": "did the work",
            "saw": list(scoped_input["data"].keys())}, "duration_s": 0.01}


def boom_runner(script, scoped_input, cfg):
    raise RuntimeError("container blew up")


def test_happy_path_full_lifecycle(ws_root):
    s = AgentAccessStore(ws_root)
    ad = FakeAdapter()
    g = s.mint("sumbot", ["pricing"])
    out = run_agent_job(s, ad, g["token"], ["pricing"], "x.py",
                        "summarize", runner=ok_runner)
    assert out["ok"] and out["scope"] == ["pricing"]
    # learning written to COMPANY memory, not carried by agent
    assert len(ad.learnings) == 1
    assert "[agent:sumbot]" in ad.learnings[0]["q"]
    # token revoked after the job (single-use)
    assert s.get(g["token"])["revoked"] is True
    events = [r["event"] for r in s.read_audit()]
    assert events == ["grant_minted", "job_started", "access_granted",
                      "learning_written", "grant_revoked", "job_completed"]


def test_out_of_scope_aborts_and_revokes(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["pricing"])
    with pytest.raises(AgentJobError):
        run_agent_job(s, FakeAdapter(), g["token"], ["salaries"], "x.py",
                      "peek", runner=ok_runner)
    assert s.get(g["token"])["revoked"] is True   # fail-closed
    ev = [r["event"] for r in s.read_audit()]
    assert "access_denied" in ev and "job_failed" in ev
    assert "job_completed" not in ev


def test_sandbox_failure_is_contained(ws_root):
    s = AgentAccessStore(ws_root)
    ad = FakeAdapter()
    g = s.mint("bot", ["pricing"])
    with pytest.raises(AgentJobError):
        run_agent_job(s, ad, g["token"], ["pricing"], "x.py", "t",
                      runner=boom_runner)
    assert ad.learnings == []                       # nothing written on failure
    assert s.get(g["token"])["revoked"] is True
    assert "job_failed" in [r["event"] for r in s.read_audit()]


def test_revoked_token_cannot_start_job(ws_root):
    s = AgentAccessStore(ws_root)
    g = s.mint("bot", ["pricing"])
    s.revoke(g["token"])
    with pytest.raises(AgentJobError):
        run_agent_job(s, FakeAdapter(), g["token"], ["pricing"], "x.py",
                      "t", runner=ok_runner)


@pytest.mark.skipif(not docker_available(), reason="docker not available")
def test_integration_real_sandbox_bundled_agent(ws_root):
    s = AgentAccessStore(ws_root)
    ad = FakeAdapter()
    g = s.mint("real-sumbot", ["pricing"])
    script = os.path.join(os.path.dirname(__file__),
                          "agentnet", "agents", "summarizer.py")
    out = run_agent_job(s, ad, g["token"], ["pricing"],
                        script, "summarize pricing")
    assert out["ok"] is True
    assert out["result"]["records_seen"] == 1
    assert len(ad.learnings) == 1
    assert s.get(g["token"])["revoked"] is True
