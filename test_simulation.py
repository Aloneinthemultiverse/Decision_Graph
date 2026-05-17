"""Tests for the Simulation Studio (local mock mode + MiroFish orchestration).

No real LLM calls, no real MiroFish backend. All external services are stubbed.
"""
import os, sys, time, traceback, json
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

PASS = "[PASS]"; FAIL = "[FAIL]"
results = {"pass": 0, "fail": 0}

def t(name):
    def wrap(fn):
        def run(*a, **kw):
            try:
                fn(*a, **kw); print(f"  {PASS} {name}"); results["pass"] += 1
            except AssertionError as e:
                print(f"  {FAIL} {name}: {e}"); results["fail"] += 1
            except Exception as e:
                print(f"  {FAIL} {name}: {type(e).__name__}: {e}")
                traceback.print_exc(); results["fail"] += 1
        return run
    return wrap


# ──────────────────────────────────────────────────────────────────────────────
# Fake LLM client — used for the local mock mode
# ──────────────────────────────────────────────────────────────────────────────
def fake_persona_client(sentiments_cycle=None):
    """Returns a fake anthropic-style client that responds with canned text.
    Each call rotates through `sentiments_cycle` to drive sentiment counts."""
    sentiments_cycle = sentiments_cycle or ["positive", "negative", "neutral"]
    counter = {"i": 0}
    class FakeResp:
        def __init__(self, text):
            class B:
                def __init__(self, t): self.type, self.text = "text", t
            self.content = [B(text)]
    class FakeMessages:
        def create(self, **kw):
            s = sentiments_cycle[counter["i"] % len(sentiments_cycle)]
            counter["i"] += 1
            return FakeResp(f"This is a thoughtful reaction.\nSentiment: {s}")
    class FakeClient:
        messages = FakeMessages()
    return FakeClient()


def fake_report_llm():
    """For the LLM-based report aggregation step."""
    class FakeResp:
        def __init__(self, text):
            class B:
                def __init__(self, t): self.type, self.text = "text", t
            self.content = [B(text)]
    payload = {
        "risks":         ["budget overrun", "market timing", "competitor response"],
        "opportunities": ["first-mover advantage", "land-grab effect"],
        "timeline":      {"day_1":"chatter","week_1":"trial buyers","month_1":"churn","month_3":"steady state"},
        "by_persona":    {"investors":"cautiously optimistic","competitors":"watching"},
    }
    class FakeMessages:
        def create(self, **kw):
            return FakeResp(json.dumps(payload))
    class FakeClient:
        messages = FakeMessages()
    return FakeClient()


# ──────────────────────────────────────────────────────────────────────────────
print("=== Local mock mode ===")

@t("SimulationManager.start returns a sim_id and starts a background thread")
def _l1():
    from decisiongraph.simulation import SimulationManager
    mgr = SimulationManager()
    sim_id = mgr.start(
        decision_text="Launch new SKU at $499.",
        personas=["investors"], depth=1,
        client=fake_persona_client(["positive"]),
    )
    assert isinstance(sim_id, str) and len(sim_id) >= 4
    # wait briefly for the thread to finish (1 persona × 1 round is fast)
    deadline = time.time() + 5
    while time.time() < deadline and mgr.get(sim_id).status == "running":
        time.sleep(0.1)
    sim = mgr.get(sim_id)
    assert sim.status == "done", f"status was {sim.status}: {sim.error}"
_l1()

@t("Local run produces depth × personas agent responses")
def _l2():
    from decisiongraph.simulation import SimulationManager
    mgr = SimulationManager()
    sim_id = mgr.start(
        decision_text="x",
        personas=["investors","customers"], depth=3,
        client=fake_persona_client(["positive","negative","neutral"]),
    )
    deadline = time.time() + 10
    while time.time() < deadline and mgr.get(sim_id).status == "running":
        time.sleep(0.1)
    sim = mgr.get(sim_id)
    assert sim.status == "done"
    assert len(sim.agent_responses) == 6, f"got {len(sim.agent_responses)}"
_l2()

@t("Sentiment parsing extracts positive/negative/neutral correctly")
def _l3():
    from decisiongraph.simulation import SimulationManager
    mgr = SimulationManager()
    sim_id = mgr.start(
        decision_text="x", personas=["investors"], depth=3,
        client=fake_persona_client(["positive","negative","neutral"]),
    )
    deadline = time.time() + 5
    while time.time() < deadline and mgr.get(sim_id).status == "running":
        time.sleep(0.1)
    sim = mgr.get(sim_id)
    sentiments = [r.sentiment for r in sim.agent_responses]
    assert "positive" in sentiments and "negative" in sentiments and "neutral" in sentiments
_l3()

@t("Report aggregates sentiment_breakdown + per_persona_counts")
def _l4():
    from decisiongraph.simulation import SimulationManager
    mgr = SimulationManager()
    # Patch _build_report to use a fake LLM for the analysis step
    sim_id = mgr.start(
        decision_text="x", personas=["investors","customers"], depth=2,
        client=fake_persona_client(["positive","negative","positive","neutral"]),
    )
    # Substitute the report-aggregation LLM
    sim = mgr.get(sim_id)
    deadline = time.time() + 5
    while time.time() < deadline and sim.status == "running":
        time.sleep(0.1)
    assert sim.status == "done"
    sb = sim.report.get("sentiment_breakdown") or {}
    assert sb.get("total") == 4
    assert sb.get("positive") + sb.get("negative") + sb.get("neutral") == 4
    bpc = sim.report.get("by_persona_counts") or {}
    assert "investors" in bpc and "customers" in bpc
_l4()

@t("Consensus score is 100 when all positive")
def _l5():
    from decisiongraph.simulation import SimulationManager
    mgr = SimulationManager()
    sim_id = mgr.start(
        decision_text="x", personas=["investors"], depth=3,
        client=fake_persona_client(["positive"]),
    )
    deadline = time.time() + 5
    while time.time() < deadline and mgr.get(sim_id).status == "running":
        time.sleep(0.1)
    sim = mgr.get(sim_id)
    assert sim.report.get("consensus_score") == 100
_l5()

@t("Empty decision_text raises ValueError")
def _l6():
    from decisiongraph.simulation import SimulationManager
    mgr = SimulationManager()
    try:
        mgr.start(decision_text="", personas=["investors"], depth=1, client=None)
        raise AssertionError("expected ValueError")
    except ValueError: pass
_l6()


# ──────────────────────────────────────────────────────────────────────────────
print("\n=== MiroFishClient orchestration (mocked HTTP) ===")

@t("MiroFishClient.run_pipeline drives the 11 expected calls in order")
def _m1():
    from decisiongraph.simulation import MiroFishClient, SimulationState
    cli = MiroFishClient("http://fake-mirofish:5001")
    call_order = []

    def rec(name, ret=None):
        def _fn(*a, **kw):
            call_order.append(name)
            return ret if ret is not None else {}
        return _fn

    cli.generate_ontology = rec("ontology", {"project_id":"proj_1","ontology":{}})
    cli.build_graph       = rec("build", "task_1")
    # task status: first call still building, second call complete
    states = [{"status":"running","progress":50},
              {"status":"completed","result":{"graph_id":"g_1"}}]
    def _ts(_t):
        call_order.append("task_status")
        return states.pop(0) if states else {"status":"completed","result":{"graph_id":"g_1"}}
    cli.task_status = _ts
    cli.create_simulation = rec("create", "sim_1")
    cli.prepare_simulation = rec("prepare")
    # prepare status: first not ready, second ready
    pstates = [{"status":"preparing"},{"status":"ready"}]
    def _ps(_s):
        call_order.append("prepare_status")
        return pstates.pop(0) if pstates else {"status":"ready"}
    cli.prepare_status = _ps
    cli.simulation_profiles_realtime = rec("profiles_rt", {"profiles":[
        {"id":"p1","name":"Alex","persona_type":"customer","background":"buyer in EMEA"},
        {"id":"p2","name":"Sam","persona_type":"investor","background":"VC partner"},
    ]})
    cli.start_simulation = rec("start")
    rstates = [{"status":"running","progress":30},{"status":"completed"}]
    def _rs(_s):
        call_order.append("run_status")
        return rstates.pop(0) if rstates else {"status":"completed"}
    cli.run_status = _rs
    cli.generate_report = rec("report.generate", {"task_id":"rtask_1","report_id":None})
    rg_states = [{"status":"running"},{"status":"completed","report_id":"rep_1"}]
    def _grs(_t):
        call_order.append("report.status")
        return rg_states.pop(0) if rg_states else {"status":"completed","report_id":"rep_1"}
    cli.report_generate_status = _grs
    cli.get_report = rec("report.fetch", {"consensus_score":72,"risks":["x"],"opportunities":["y"]})

    sim = SimulationState(id="t1", decision_text="x", personas=[], depth=1, company_id="")
    stages = []; agents = []
    report = cli.run_pipeline(sim, on_stage=lambda s,p: stages.append((s,p)),
                               on_agent=lambda a: agents.append(a),
                               poll_interval=0.01)

    # Verify the major calls happened in the right order
    expected_anchors = ["ontology","build","task_status","create","prepare","prepare_status",
                        "start","run_status","report.generate","report.status","report.fetch"]
    seq = [c for c in call_order if c in expected_anchors]
    # de-duplicate consecutive duplicates from polling
    dedup = []
    for c in seq:
        if not dedup or dedup[-1] != c: dedup.append(c)
    assert dedup == expected_anchors, f"got order: {dedup}"
    # Agents streamed in from prepare profiles
    assert len(agents) == 2, f"expected 2 agents from profiles, got {len(agents)}"
    # Report returned
    assert report["consensus_score"] == 72
    # MiroFish IDs recorded
    assert sim.mirofish["project_id"] == "proj_1"
    assert sim.mirofish["graph_id"]   == "g_1"
    assert sim.mirofish["simulation_id"] == "sim_1"
    assert sim.mirofish["report_id"]  == "rep_1"
_m1()


@t("MiroFishClient.run_pipeline raises when graph build fails")
def _m2():
    from decisiongraph.simulation import MiroFishClient, SimulationState
    cli = MiroFishClient("http://fake")
    cli.generate_ontology = lambda **kw: {"project_id":"p","ontology":{}}
    cli.build_graph = lambda pid: "t1"
    cli.task_status = lambda tid: {"status":"failed","error":"out of memory"}
    sim = SimulationState(id="x", decision_text="y", personas=[], depth=1, company_id="")
    try:
        cli.run_pipeline(sim, on_stage=lambda *a: None, on_agent=lambda *a: None,
                          poll_interval=0.01)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "Graph build failed" in str(e)
_m2()


@t("Manager picks MiroFish mode when url is set")
def _m3():
    from decisiongraph.simulation import SimulationManager
    mgr = SimulationManager()
    with patch("decisiongraph.simulation.MiroFishClient") as fakecls:
        inst = fakecls.return_value
        inst.health.return_value = {"status":"ok"}
        inst.run_pipeline.return_value = {"consensus_score": 60}
        sim_id = mgr.start(
            decision_text="x", personas=["investors"], depth=1,
            client=fake_report_llm(), mirofish_url="http://fake:5001",
        )
        deadline = time.time() + 5
        while time.time() < deadline and mgr.get(sim_id).status == "running":
            time.sleep(0.05)
        sim = mgr.get(sim_id)
        assert sim.mode == "mirofish"
        assert sim.status == "done", sim.error
        assert sim.report.get("mirofish_raw", {}).get("consensus_score") == 60
        assert inst.health.called
        assert inst.run_pipeline.called
_m3()


@t("Manager falls back to error when MiroFish health check fails")
def _m4():
    from decisiongraph.simulation import SimulationManager
    mgr = SimulationManager()
    with patch("decisiongraph.simulation.MiroFishClient") as fakecls:
        inst = fakecls.return_value
        inst.health.side_effect = Exception("connection refused")
        sim_id = mgr.start(
            decision_text="x", personas=["investors"], depth=1,
            client=fake_report_llm(), mirofish_url="http://fake:5001",
        )
        deadline = time.time() + 5
        while time.time() < deadline and mgr.get(sim_id).status == "running":
            time.sleep(0.05)
        sim = mgr.get(sim_id)
        assert sim.status == "error"
        assert "health check failed" in sim.error.lower()
_m4()


# ──────────────────────────────────────────────────────────────────────────────
print(f"\n=== Results: {results['pass']} passed, {results['fail']} failed ===")
sys.exit(0 if results["fail"] == 0 else 1)
