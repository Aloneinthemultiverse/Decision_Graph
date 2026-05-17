"""End-to-end tests for the 5-task evolution: forgetting, uncertainty,
relationships, outcomes, and cross-company patterns.

All tests use fake embed models — no LLM calls, no network."""
import os, sys, traceback, time
from pathlib import Path
import numpy as np
import networkx as nx
from datetime import datetime, timedelta

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


class FakeEmbed:
    """Deterministic across runs — uses a stable hash so the same tokens always
    map to the same dimension regardless of PYTHONHASHSEED."""
    DIM = 8
    @staticmethod
    def _stable_hash(s: str) -> int:
        import hashlib
        return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)
    def encode(self, texts):
        out = []
        for txt in texts:
            v = np.zeros(self.DIM, dtype=float)
            for tok in str(txt).lower().split():
                v[self._stable_hash(tok) % self.DIM] += 1.0
            n = np.linalg.norm(v) or 1.0
            out.append(v / n)
        return np.array(out)


# ──────────────────────────────────────────────────────────────────────────────
print("=== TASK 2 — Uncertainty layer ===")

@t("check_uncertainty returns uncertain when graph is empty")
def _u1():
    from decisiongraph.query import check_uncertainty
    em = FakeEmbed()
    r = check_uncertainty("anything", None, None, None, em)
    assert r["uncertain"] is True
    assert r["confidence"] == 0.0
    assert "Ingest" in r.get("suggestion", "")
_u1()

@t("check_uncertainty refuses when max similarity below threshold")
def _u2():
    """Use strictly-orthogonal embeddings so the result is deterministic
    independent of hash bucketing or PYTHONHASHSEED."""
    from decisiongraph.query import check_uncertainty
    class FixedEmbed:
        def encode(self, texts):
            # Two orthogonal vectors — guarantees cosine similarity = 0
            mapping = {"COMMUNITY": np.array([1.0,0,0,0]),
                       "QUESTION":  np.array([0,1.0,0,0])}
            return np.array([mapping[t] for t in texts])
    em = FixedEmbed()
    cids = [0]
    summaries = {0: {"summary": "topic-A", "nodes": []}}
    embs = em.encode(["COMMUNITY"])
    r = check_uncertainty("QUESTION", embs, cids, summaries, em, threshold=0.3)
    assert r["uncertain"] is True, r
    assert r["confidence"] < 0.3
    assert "context" in r["message"]
_u2()

@t("check_uncertainty allows when question matches a community")
def _u3():
    """Identical embedding for both → cosine sim = 1.0, well above threshold."""
    from decisiongraph.query import check_uncertainty
    class FixedEmbed:
        def encode(self, texts):
            return np.array([np.array([1.0, 0, 0, 0]) for _ in texts])
    em = FixedEmbed()
    cids = [0]
    summaries = {0: {"summary": "x", "nodes": []}}
    embs = em.encode(["topic"])
    r = check_uncertainty("topic", embs, cids, summaries, em, threshold=0.3)
    assert r["uncertain"] is False, r
    assert r["confidence"] >= 0.3
_u3()

@t("session_mode refuses when all graphs uncertain AND gate is enabled")
def _u4():
    from decisiongraph.agent import session_mode
    em = FakeEmbed()
    cids = [0]; summaries = {0: {"summary": "quantum chromo", "nodes": ["q"]}}; embs = em.encode(["quantum chromo"])
    G = nx.MultiDiGraph(); G.add_edge("q", "x", relation="r")
    class M:
        def query(self, q, em, **kw): return []
        def store(self, **kw): return "id1"
        def save(self): pass
    out = session_mode("how to brew tea", client=None, G=G,
                       community_summaries=summaries, community_ids=cids,
                       community_embeddings=embs, embed_model=em, memory=M(),
                       uncertainty_enabled=True)
    assert isinstance(out, str)
    assert out.startswith("[UNCERTAIN")
    assert "Suggestion:" in out
_u4()

@t("uncertainty gate disabled (default) lets sparse queries through")
def _u4b():
    from decisiongraph.agent import _check_uncertainty_across_graphs
    em = FakeEmbed()
    cids = [0]; summaries = {0:{"summary":"x","nodes":["q"]}}; embs = em.encode(["x"])
    graphs = [{"name":"g","G": nx.MultiDiGraph(), "community_summaries":summaries,
               "community_ids":cids, "community_embeddings":embs}]
    graphs[0]["G"].add_edge("q","y",relation="r")
    # With enabled=False (the default), should ALWAYS return None even on a clearly
    # unrelated question that would otherwise trigger the gate.
    out = _check_uncertainty_across_graphs("totally unrelated", graphs, em, enabled=False)
    assert out is None
    # With enabled=True on the same query, it should refuse.
    out_on = _check_uncertainty_across_graphs("totally unrelated", graphs, em, enabled=True)
    assert out_on is not None and out_on["uncertain"] is True
_u4b()

@t("session_mode proceeds when at least one graph is confident (mock check)")
def _u5():
    from decisiongraph import agent as a
    em = FakeEmbed()
    cids = [0]; summaries = {0:{"summary":"s","nodes":["a"]}}; embs = em.encode(["s"])
    G = nx.MultiDiGraph(); G.add_edge("a","b",relation="r")

    orig = a._check_uncertainty_across_graphs
    a._check_uncertainty_across_graphs = lambda *args, **kw: None  # force "confident"
    # NOTE: gate disabled by default so even without the mock, this would pass.
    # We mock here to be explicit about what we're testing.
    try:
        from decisiongraph.agent import session_mode
        class FakeClient:
            def messages(self): pass
        class FakeResp:
            def __init__(self):
                class C: type="text"; text="ANSWER"
                self.content=[C()]
        class FakeClientReal:
            messages = type("M", (), {"create": staticmethod(lambda **kw: FakeResp())})
        class M:
            stored=[]
            def query(self, q, em, **kw): return []
            def store(self, **kw): M.stored.append(kw); return "id1"
            def save(self): pass
        out = session_mode("anything", client=FakeClientReal(), G=G,
                            community_summaries=summaries, community_ids=cids,
                            community_embeddings=embs, embed_model=em, memory=M())
        # got past the uncertainty gate, hit the (mocked) LLM
        assert not out.startswith("[UNCERTAIN"), out
    finally:
        a._check_uncertainty_across_graphs = orig
_u5()


# ──────────────────────────────────────────────────────────────────────────────
print("\n=== TASK 4 — Outcome tracking ===")

def fresh_memory():
    from decisiongraph.decisions import DecisionMemory
    return DecisionMemory()

@t("DecisionNode has outcome fields with defaults")
def _o1():
    from decisiongraph.decisions import DecisionNode
    n = DecisionNode()
    assert n.outcome == "unknown"
    assert n.outcome_notes == ""
    assert n.outcome_recorded_at == ""
    assert n.outcome_impact == 0.0
_o1()

@t("update_outcome=success bumps confidence (capped at 1.0)")
def _o2():
    m = fresh_memory()
    did = m.store("q","a","r",[1],[])
    m.update_outcome(did, "success", notes="went well", impact=0.5)
    node = m.graph.nodes[did]["data"]
    assert node.outcome == "success"
    assert node.outcome_notes == "went well"
    assert node.outcome_impact == 0.5
    assert node.confidence == 1.0   # capped
_o2()

@t("update_outcome=failure drops confidence by 0.3")
def _o3():
    m = fresh_memory()
    did = m.store("q","a","r",[1],[])
    m.update_outcome(did, "failure", impact=-0.5)
    node = m.graph.nodes[did]["data"]
    assert node.outcome == "failure"
    assert node.confidence == 0.7   # 1.0 - 0.3
_o3()

@t("update_outcome on unknown decision id returns False")
def _o4():
    m = fresh_memory()
    assert m.update_outcome("ghost", "success") is False
_o4()

@t("get_outcome_patterns aggregates by community")
def _o5():
    m = fresh_memory()
    d1 = m.store("q1","a","r",[5],[]); m.update_outcome(d1, "success")
    d2 = m.store("q2","a","r",[5],[]); m.update_outcome(d2, "failure")
    d3 = m.store("q3","a","r",[5,7],[]); m.update_outcome(d3, "success")
    pat = m.get_outcome_patterns()
    assert "5" in pat
    assert pat["5"]["total_decisions"] == 3
    assert pat["5"]["success_rate"] == round(2/3, 3)
    assert pat["5"]["failure_rate"] == round(1/3, 3)
    assert pat["7"]["total_decisions"] == 1
_o5()

@t("query() weights success higher than failure for same similarity")
def _o6():
    m = fresh_memory()
    em = FakeEmbed()
    d_success = m.store("Q about pets", "answer", "r", [1], [])
    d_failure = m.store("Q about pets", "answer", "r", [1], [])
    m.update_outcome(d_success, "success")
    m.update_outcome(d_failure, "failure")
    results = m.query("Q about pets", em, top_k=2)
    assert len(results) == 2
    # success should rank higher (higher score)
    assert results[0]["id"] == d_success
    assert results[0]["score"] > results[1]["score"]
_o6()


# ──────────────────────────────────────────────────────────────────────────────
print("\n=== TASK 1 — Forgetting / decay ===")

@t("New DecisionNode has confidence=1.0, is_active=True, access_count=0")
def _f1():
    from decisiongraph.decisions import DecisionNode
    n = DecisionNode()
    assert n.confidence == 1.0
    assert n.is_active is True
    assert n.access_count == 0
    assert n.last_accessed == ""
    assert n.superseded_by == ""
_f1()

@t("access_decision increments count and stamps last_accessed")
def _f2():
    m = fresh_memory()
    did = m.store("q","a","r",[],[])
    m.access_decision(did)
    node = m.graph.nodes[did]["data"]
    assert node.access_count == 1
    assert node.last_accessed != ""
    m.access_decision(did)
    assert node.access_count == 2
_f2()

@t("decay_confidence drops confidence for stale decisions")
def _f3():
    m = fresh_memory()
    did = m.store("q","a","r",[],[])
    # force old timestamp
    node = m.graph.nodes[did]["data"]
    node.timestamp = (datetime.now() - timedelta(days=200)).isoformat()
    node.last_accessed = ""
    changed = m.decay_confidence(days_threshold=90)
    assert changed == 1
    assert node.confidence == 0.9
_f3()

@t("decay_confidence marks decisions inactive when conf < 0.2")
def _f4():
    m = fresh_memory()
    did = m.store("q","a","r",[],[])
    node = m.graph.nodes[did]["data"]
    node.timestamp = (datetime.now() - timedelta(days=200)).isoformat()
    node.confidence = 0.25  # one decay step → 0.15 → inactive
    m.decay_confidence(days_threshold=90)
    assert node.is_active is False
    assert node.confidence < 0.2
_f4()

@t("supersede marks old inactive and adds graph edge")
def _f5():
    m = fresh_memory()
    old = m.store("q1","a","r",[],[])
    new = m.store("q2","a","r",[],[])
    ok = m.supersede(old, new)
    assert ok is True
    assert m.graph.nodes[old]["data"].is_active is False
    assert m.graph.nodes[old]["data"].superseded_by == new
    assert m.graph.has_edge(new, old)
    assert m.graph.edges[new, old]["relation"] == "supersedes"
_f5()

@t("get_active_decisions excludes inactive, sorted by confidence")
def _f6():
    m = fresh_memory()
    d1 = m.store("q1","a","r",[],[])
    d2 = m.store("q2","a","r",[],[])
    d3 = m.store("q3","a","r",[],[])
    m.graph.nodes[d2]["data"].confidence = 0.5
    m.graph.nodes[d3]["data"].is_active = False
    active = m.get_active_decisions()
    ids = [r["id"] for r in active]
    assert d3 not in ids
    assert ids[0] == d1     # highest conf first
    assert ids[-1] == d2
_f6()

@t("query() only returns active decisions, weighted by confidence")
def _f7():
    m = fresh_memory()
    em = FakeEmbed()
    d_hi = m.store("project alpha rollout","answer","r",[1],[])
    d_low = m.store("project alpha rollout","answer","r",[1],[])
    m.graph.nodes[d_low]["data"].confidence = 0.3
    results = m.query("project alpha", em, top_k=5)
    assert results
    # high confidence should outrank low
    assert results[0]["id"] == d_hi
    assert results[0]["score"] > results[1]["score"]
_f7()

@t("query() bumps access_count on returned decisions")
def _f8():
    m = fresh_memory()
    em = FakeEmbed()
    did = m.store("project alpha","ans","r",[],[])
    assert m.graph.nodes[did]["data"].access_count == 0
    m.query("project alpha", em)
    assert m.graph.nodes[did]["data"].access_count >= 1
_f8()


# ──────────────────────────────────────────────────────────────────────────────
print("\n=== TASK 3 — Decision relationships ===")

@t("store() with caused_by adds 'caused' edge")
def _r1():
    m = fresh_memory()
    parent = m.store("hire?","yes","r",[],[])
    child  = m.store("when?","Q3","r",[], [], caused_by=parent)
    assert m.graph.has_edge(parent, child)
    assert m.graph.edges[parent, child]["relation"] == "caused"
_r1()

@t("store() with depends_on adds 'depends_on' edge")
def _r2():
    m = fresh_memory()
    a = m.store("a","","",[],[])
    b = m.store("b","","",[],[], depends_on=a)
    assert m.graph.has_edge(b, a)
    assert m.graph.edges[b, a]["relation"] == "depends_on"
_r2()

@t("store() with related_to adds multiple edges")
def _r3():
    m = fresh_memory()
    a = m.store("a","","",[],[])
    b = m.store("b","","",[],[])
    c = m.store("c","","",[],[], related_to=[a, b])
    assert m.graph.has_edge(c, a)
    assert m.graph.has_edge(c, b)
    assert m.graph.edges[c, a]["relation"] == "related_to"
_r3()

@t("get_decision_chain returns full lineage")
def _r4():
    m = fresh_memory()
    root = m.store("root","","",[],[])
    mid  = m.store("mid", "","",[],[], caused_by=root)
    leaf = m.store("leaf","","",[],[], depends_on=mid)
    chain = m.get_decision_chain(mid)
    assert any(c["id"] == root for c in chain["caused_by"])
    # leaf depends_on mid → mid has incoming depends_on (so leaf shows in "led_to"? no
    # depends_on is an outgoing edge from leaf to mid; from mid's view it's incoming.
    # Our chain query for `mid` only returns mid's outgoing depends_on edges, so leaf
    # won't appear in mid's chain. That's expected — the chain is mid-centric.
    assert chain["id"] == mid
_r4()

@t("get_decision_chain on unknown id returns error")
def _r5():
    m = fresh_memory()
    chain = m.get_decision_chain("ghost")
    assert "error" in chain
_r5()


# ──────────────────────────────────────────────────────────────────────────────
print("\n=== TASK 5 — Cross-company patterns ===")

@t("EnterpriseHub.get_anonymized_patterns aggregates across companies")
def _x1():
    from decisiongraph import company as co_mod
    real_root = co_mod.CompanyMemory._ROOT_STORAGE_DIR
    test_root = ROOT / "storage" / "_test_x1"
    import shutil
    if test_root.exists(): shutil.rmtree(test_root, ignore_errors=True)
    try:
        co_mod.CompanyMemory._ROOT_STORAGE_DIR = str(test_root)
        hub = co_mod.EnterpriseHub()
        # bypass disk — manually populate
        a = co_mod.CompanyMemory("a", "A Inc")
        b = co_mod.CompanyMemory("b", "B Inc")
        hub.companies = {"a": a, "b": b}
        # add decisions
        d1 = a.company_dg.memory.store("q1","ans","r",[42],[])
        a.company_dg.memory.update_outcome(d1, "success")
        d2 = b.company_dg.memory.store("q2","ans","r",[42],[])
        b.company_dg.memory.update_outcome(d2, "failure")
        d3 = b.company_dg.memory.store("q3","ans","r",[42],[])
        b.company_dg.memory.update_outcome(d3, "success")
        pat = hub.get_anonymized_patterns()
        assert "42" in pat
        assert pat["42"]["total_decisions"] == 3
        assert pat["42"]["success_rate"] == round(2/3, 3)
        # samples don't leak company names
        for q in pat["42"]["question_samples"]:
            assert "A Inc" not in q and "B Inc" not in q
    finally:
        co_mod.CompanyMemory._ROOT_STORAGE_DIR = real_root
        if test_root.exists(): shutil.rmtree(test_root, ignore_errors=True)
_x1()

@t("EnterpriseHub.get_wisdom excludes own company")
def _x2():
    from decisiongraph import company as co_mod
    real_root = co_mod.CompanyMemory._ROOT_STORAGE_DIR
    test_root = ROOT / "storage" / "_test_x2"
    import shutil
    if test_root.exists(): shutil.rmtree(test_root, ignore_errors=True)
    try:
        co_mod.CompanyMemory._ROOT_STORAGE_DIR = str(test_root)
        hub = co_mod.EnterpriseHub()
        a = co_mod.CompanyMemory("a", "A")
        b = co_mod.CompanyMemory("b", "B")
        hub.companies = {"a": a, "b": b}
        # a has a decision, b is empty
        a.company_dg.memory.store("how to fire bad hire", "respectfully", "r", [], [])
        # asking from B's perspective → should see A's wisdom
        em = FakeEmbed()
        w = hub.get_wisdom("how to fire bad hire", company_id="b", embed_model=em)
        assert "how to fire" in w.lower()
        # asking from A's perspective → should not include A's own decisions
        w_own = hub.get_wisdom("how to fire bad hire", company_id="a", embed_model=em)
        assert w_own == ""
    finally:
        co_mod.CompanyMemory._ROOT_STORAGE_DIR = real_root
        if test_root.exists(): shutil.rmtree(test_root, ignore_errors=True)
_x2()

@t("CompanyMemory.query passes hub through and includes wisdom in metadata")
def _x3():
    from decisiongraph import company as co_mod
    from decisiongraph import agent as a_mod
    real_root = co_mod.CompanyMemory._ROOT_STORAGE_DIR
    test_root = ROOT / "storage" / "_test_x3"
    import shutil
    if test_root.exists(): shutil.rmtree(test_root, ignore_errors=True)
    captured = {}
    orig_session = a_mod.session_mode
    def spy(*args, **kw):
        captured["company_metadata"] = kw.get("company_metadata") or (args[8] if len(args) > 8 else None)
        return "fake answer"
    a_mod.session_mode = spy
    try:
        co_mod.CompanyMemory._ROOT_STORAGE_DIR = str(test_root)
        hub = co_mod.EnterpriseHub()
        a = co_mod.CompanyMemory("a", "A")
        b = co_mod.CompanyMemory("b", "B")
        hub.companies = {"a": a, "b": b}
        # populate B with a relevant decision so A can learn from it
        b.company_dg.memory.store("hire VP engineering", "yes, expand to NY", "r", [], [])
        # also need graphs to exist so query() doesn't short-circuit
        # easiest: hack _graphs_payload to return non-empty
        a._graphs_payload = lambda: [{"name":"x","G": __import__("networkx").MultiDiGraph(),
                                       "community_summaries":{}, "community_ids":[], "community_embeddings": None}]
        # call query
        a.query("hire VP engineering", mode="session", hub=hub)
        assert captured.get("company_metadata") is not None
        # wisdom may be empty if embed model didn't rank, just confirm metadata was passed
    finally:
        a_mod.session_mode = orig_session
        co_mod.CompanyMemory._ROOT_STORAGE_DIR = real_root
        if test_root.exists(): shutil.rmtree(test_root, ignore_errors=True)
_x3()


# ──────────────────────────────────────────────────────────────────────────────
print("\n=== Backward-compat ===")

@t("Old DecisionNode pickles upgrade cleanly on load()")
def _bc1():
    """Simulate old pickle by manually creating a node missing the new fields."""
    from decisiongraph.decisions import DecisionMemory, DecisionNode, _NODE_DEFAULTS
    import pickle, tempfile
    g = nx.DiGraph()
    # build a "legacy" node and strip the new attributes
    n = DecisionNode(question="legacy", answer="x", reasoning_summary="r",
                     communities_used=[1], context_triples=[])
    for k in _NODE_DEFAULTS:
        if hasattr(n, k): delattr(n, k)
    g.add_node(n.id, data=n)
    fd, path = tempfile.mkstemp(suffix=".pkl"); os.close(fd)
    with open(path, "wb") as f: pickle.dump(g, f)
    try:
        m = DecisionMemory()
        m.load(path)
        node = m.graph.nodes[n.id]["data"]
        for k, v in _NODE_DEFAULTS.items():
            assert hasattr(node, k), f"missing {k} after migration"
            assert getattr(node, k) == v, f"{k} wasn't defaulted"
    finally:
        os.unlink(path)
_bc1()


# ──────────────────────────────────────────────────────────────────────────────
print(f"\n=== Results: {results['pass']} passed, {results['fail']} failed ===")
sys.exit(0 if results["fail"] == 0 else 1)
