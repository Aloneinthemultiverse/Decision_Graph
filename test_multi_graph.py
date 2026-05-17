"""Tests for multi_graph_beam_query + CompanyMemory dual-graph + agent modes.

Heavy lifting (real LLM call, real ingestion) is avoided where possible by
constructing fake graph objects with the same shape beam_query expects.

Run:
    python test_multi_graph.py
"""
import os, sys, traceback
import numpy as np
import networkx as nx
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from decisiongraph.query import beam_query, multi_graph_beam_query
from decisiongraph import agent as agent_mod

PASS = "[PASS]"
FAIL = "[FAIL]"
results = {"pass": 0, "fail": 0}

def t(name):
    def wrap(fn):
        def run(*a, **kw):
            try:
                fn(*a, **kw)
                print(f"  {PASS} {name}"); results["pass"] += 1
            except AssertionError as e:
                print(f"  {FAIL} {name}: {e}"); results["fail"] += 1
            except Exception as e:
                print(f"  {FAIL} {name}: {type(e).__name__}: {e}")
                traceback.print_exc(); results["fail"] += 1
        return run
    return wrap

# ──────────────────────────────────────────────────────────────────────────────
# Fakes — minimal stand-ins for embeddings & graphs
# ──────────────────────────────────────────────────────────────────────────────
class FakeEmbedModel:
    """Deterministic across runs — uses a stable hash so test results are
    reproducible regardless of PYTHONHASHSEED."""
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

def make_graph(triples):
    """Build a directed multigraph from (u, relation, v) tuples."""
    G = nx.MultiDiGraph()
    for u, rel, v in triples:
        G.add_edge(u, v, relation=rel)
    return G

def make_payload(name: str, triples: list, community_summary: str):
    """Wrap a graph + a single community into the dict shape multi_graph_beam_query expects."""
    em = FakeEmbedModel()
    G = make_graph(triples)
    nodes = list(G.nodes())
    summaries = {0: {"summary": community_summary, "nodes": nodes}}
    cids = [0]
    cembs = em.encode([community_summary])
    return {"name": name, "G": G, "community_summaries": summaries,
            "community_ids": cids, "community_embeddings": cembs}, em


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────
print("=== multi_graph_beam_query ===")

@t("Returns merged + tagged context from two graphs")
def _t1():
    pA, em = make_payload(
        "Knowledge Graph",
        [("transformer", "uses", "attention"), ("attention", "scales", "quadratically")],
        "transformer attention mechanisms",
    )
    pB, _ = make_payload(
        "Company Memory",
        [("project alpha", "uses", "transformer"), ("project alpha", "owned_by", "ml team")],
        "project alpha team ownership",
    )
    triples, report = multi_graph_beam_query("transformer", [pA, pB], em)
    # both graphs should contribute
    assert "Knowledge Graph" in report and "Company Memory" in report, report
    # all triples should be tagged with their source
    assert all(t.startswith("[Knowledge Graph] ") or t.startswith("[Company Memory] ") for t in triples), triples
    # at least one triple from each
    assert any(t.startswith("[Knowledge Graph]") for t in triples)
    assert any(t.startswith("[Company Memory]") for t in triples)
_t1()

@t("Dedupes identical raw triples across graphs")
def _t2():
    # same fact appears in both graphs
    pA, em = make_payload("A", [("x", "is", "y")], "x is y")
    pB, _  = make_payload("B", [("x", "is", "y")], "x is y")
    triples, report = multi_graph_beam_query("x", [pA, pB], em)
    # only one tagged version should remain (first wins)
    occurrences = [t for t in triples if t.endswith("x --[is]--> y")]
    assert len(occurrences) == 1, occurrences
    # one graph reports 1 new triple, the other 0
    counts = sorted([report["A"]["triples"], report["B"]["triples"]])
    assert counts == [0, 1], counts
_t2()

@t("Handles empty graphs without crashing")
def _t3():
    em = FakeEmbedModel()
    empty = {"name": "Empty", "G": None, "community_summaries": {}, "community_ids": [], "community_embeddings": None}
    pA, _ = make_payload("Real", [("a", "rel", "b")], "a rel b")
    triples, report = multi_graph_beam_query("a", [empty, pA], em)
    assert report["Empty"]["skipped"] == "graph is empty"
    assert report["Real"]["triples"] >= 1
_t3()

@t("Handles graph with zero nodes")
def _t4():
    em = FakeEmbedModel()
    empty_G = nx.MultiDiGraph()  # no nodes
    g = {"name": "Zero", "G": empty_G, "community_summaries": {0:{"summary":"x","nodes":[]}},
         "community_ids":[0], "community_embeddings": em.encode(["x"])}
    triples, report = multi_graph_beam_query("foo", [g], em)
    assert triples == []
    assert report["Zero"]["skipped"] == "graph is empty"
_t4()

@t("Returns empty when no graphs given")
def _t5():
    em = FakeEmbedModel()
    triples, report = multi_graph_beam_query("anything", [], em)
    assert triples == [] and report == {}
_t5()

# ──────────────────────────────────────────────────────────────────────────────
print("\n=== agent helpers ===")

@t("_normalize_graphs returns the explicit list when given")
def _t6():
    g = [{"name":"x", "G": make_graph([("a","r","b")]), "community_summaries":{}, "community_ids":[], "community_embeddings": None}]
    out = agent_mod._normalize_graphs(g, None, None, None, None)
    assert out is g
_t6()

@t("_normalize_graphs wraps old args into single-element list")
def _t7():
    G = make_graph([("a","r","b")])
    out = agent_mod._normalize_graphs(None, G, {0:{"summary":"s","nodes":["a"]}}, [0], np.zeros((1,8)))
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["name"] == "Knowledge Graph"
    assert out[0]["G"] is G
_t7()

@t("_normalize_graphs returns [] when no graph data")
def _t8():
    assert agent_mod._normalize_graphs(None, None, None, None, None) == []
_t8()

@t("_flatten_communities tags community ids with graph name")
def _t9():
    report = {"Knowledge Graph": {"communities":[0,1], "triples":3},
              "Company Memory":  {"communities":[5],   "triples":2}}
    out = agent_mod._flatten_communities(report)
    assert "Knowledge Graph:0" in out and "Knowledge Graph:1" in out and "Company Memory:5" in out
    assert len(out) == 3
_t9()

# ──────────────────────────────────────────────────────────────────────────────
print("\n=== CompanyMemory dual-graph structure ===")

@t("CompanyMemory exposes knowledge_dg and company_dg with isolated storage dirs")
def _t10():
    # set a clean test dir so we don't disturb real data
    from decisiongraph import company as co_mod
    real_root = co_mod.CompanyMemory._ROOT_STORAGE_DIR
    test_root = ROOT / "storage" / "_test_multigraph"
    try:
        co_mod.CompanyMemory._ROOT_STORAGE_DIR = str(test_root)
        cm = co_mod.CompanyMemory("test_co", "Test Co")
        assert hasattr(cm, "knowledge_dg")
        assert hasattr(cm, "company_dg")
        assert cm.dg is cm.company_dg, "cm.dg should alias company_dg for backward compat"
        assert cm.knowledge_dir.endswith("knowledge"), cm.knowledge_dir
        assert cm.company_subdir.endswith("company"),  cm.company_subdir
        assert os.path.isdir(cm.knowledge_dir)
        assert os.path.isdir(cm.company_subdir)
        # _graphs_payload should be empty (no ingest yet)
        assert cm._graphs_payload() == []
        # stats should report both
        s = cm.stats()
        assert "knowledge_graph" in s and "company_graph" in s
    finally:
        co_mod.CompanyMemory._ROOT_STORAGE_DIR = real_root
        # cleanup
        import shutil
        if test_root.exists(): shutil.rmtree(test_root, ignore_errors=True)
_t10()

@t("CompanyMemory has ingest_knowledge and ingest_company methods")
def _t11():
    from decisiongraph.company import CompanyMemory
    assert callable(getattr(CompanyMemory, "ingest_knowledge", None))
    assert callable(getattr(CompanyMemory, "ingest_company", None))
    assert callable(getattr(CompanyMemory, "ingest", None))   # backward compat
_t11()

# ──────────────────────────────────────────────────────────────────────────────
print(f"\n=== Results: {results['pass']} passed, {results['fail']} failed ===")
sys.exit(0 if results["fail"] == 0 else 1)
