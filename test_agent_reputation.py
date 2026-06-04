"""Phase 2 slice-1 proof: reputation is computed from the immutable audit log.

Run: python -m pytest test_agent_reputation.py -q
"""
import shutil
import tempfile

import pytest

from decisiongraph.agent_access import AgentAccessStore
from decisiongraph.agent_job import run_agent_job
from decisiongraph.agent_reputation import compute_reputation


@pytest.fixture()
def ws_root():
    d = tempfile.mkdtemp(prefix="dg_ws_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class FakeAdapter:
    def __init__(self):
        self.learnings = []

    def fetch_scope(self, topic):
        return [{"question": "q", "answer": "a", "confidence": 1.0}]

    def store_learning(self, question, answer, reasoning):
        self.learnings.append(answer)
        return f"L{len(self.learnings)}"


def ok_runner(s, i, c):
    return {"ok": True, "result": {"answer": "done"}, "duration_s": 0.01}


def _rep_map(store):
    return {r["agent_name"]: r for r in compute_reputation(store)}


def test_empty_audit_has_no_reputation(ws_root):
    assert compute_reputation(AgentAccessStore(ws_root)) == []


def test_successful_agent_gains_reputation(ws_root):
    s = AgentAccessStore(ws_root)
    ad = FakeAdapter()
    for _ in range(3):
        g = s.mint("good-bot", ["pricing"])
        run_agent_job(s, ad, g["token"], ["pricing"], "x.py", "t",
                      runner=ok_runner)
    r = _rep_map(s)["good-bot"]
    # 3 completed (+36) + 3 learnings (+9) on base 50
    assert r["jobs_completed"] == 3
    assert r["learnings_written"] == 3
    assert r["score"] == 95
    assert r["standing"] == "trusted"


def test_scope_violator_loses_reputation(ws_root):
    s = AgentAccessStore(ws_root)
    ad = FakeAdapter()
    for _ in range(2):
        g = s.mint("bad-bot", ["pricing"])
        with pytest.raises(Exception):
            run_agent_job(s, ad, g["token"], ["salaries"], "x.py", "t",
                          runner=ok_runner)
    r = _rep_map(s)["bad-bot"]
    # 2 denied (-30) + 2 failed (-20) on base 50 -> clamped to 0
    assert r["access_denied"] == 2
    assert r["jobs_failed"] == 2
    assert r["score"] == 0
    assert r["standing"] == "untrusted"


def test_reputation_is_purely_derived_not_stored(ws_root):
    # computing twice yields identical results; nothing persisted/mutated
    s = AgentAccessStore(ws_root)
    ad = FakeAdapter()
    g = s.mint("bot", ["pricing"])
    run_agent_job(s, ad, g["token"], ["pricing"], "x.py", "t",
                  runner=ok_runner)
    assert compute_reputation(s) == compute_reputation(s)


def test_ranking_orders_by_score(ws_root):
    s = AgentAccessStore(ws_root)
    ad = FakeAdapter()
    g = s.mint("hero", ["pricing"])
    run_agent_job(s, ad, g["token"], ["pricing"], "x.py", "t", runner=ok_runner)
    g2 = s.mint("villain", ["pricing"])
    with pytest.raises(Exception):
        run_agent_job(s, ad, g2["token"], ["secret"], "x.py", "t",
                      runner=ok_runner)
    ranked = [r["agent_name"] for r in compute_reputation(s)]
    assert ranked.index("hero") < ranked.index("villain")
