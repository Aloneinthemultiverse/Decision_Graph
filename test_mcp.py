"""Tests for the refactored, company-aware MCP server."""
import os, sys, asyncio, traceback
from pathlib import Path
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

print("=== Structural ===")

@t("Module imports without side-effects (no DG construction at import)")
def _t1():
    import decisiongraph.mcp_server as m
    assert m.TOOL_DEFS is not None
    assert m.TOOL_HANDLERS is not None
    assert callable(m.invoke_tool)
_t1()

@t("All 7 tools defined (6 originals + recall)")
def _t2():
    import decisiongraph.mcp_server as m
    names = {t.name for t in m.TOOL_DEFS}
    expected = {"list_companies","query_knowledge","get_past_decisions",
                "store_decision","ingest_document","get_stats","recall"}
    assert names == expected, f"got {names}"
_t2()

@t("Every tool has a registered handler")
def _t3():
    import decisiongraph.mcp_server as m
    for tool in m.TOOL_DEFS:
        assert tool.name in m.TOOL_HANDLERS, f"{tool.name} missing handler"
_t3()

@t("Every tool except list_companies/get_stats supports `company_id`")
def _t4():
    import decisiongraph.mcp_server as m
    optional_co = {"query_knowledge","get_past_decisions","store_decision","ingest_document","get_stats"}
    for tool in m.TOOL_DEFS:
        if tool.name == "list_companies": continue
        props = tool.inputSchema.get("properties", {})
        assert "company_id" in props, f"{tool.name} missing company_id"
_t4()

@t("ingest_document supports `target` enum [knowledge, company]")
def _t5():
    import decisiongraph.mcp_server as m
    tool = next(x for x in m.TOOL_DEFS if x.name == "ingest_document")
    props = tool.inputSchema["properties"]
    assert "target" in props
    assert set(props["target"]["enum"]) == {"knowledge","company"}
_t5()

print("\n=== Handlers (with fake state) ===")

# Build minimal fakes — no real LLM / no real graph
import networkx as nx

class FakeMemory:
    def __init__(self): self.stored=[]
    def query(self, q, em): return []
    def store(self, **kw): self.stored.append(kw); return f"d{len(self.stored)}"
    def save(self): pass

class FakeDG:
    def __init__(self):
        self.G = None
        self.summaries = {}; self.community_ids = []; self.community_embeddings = None
        self.embed_model = None
        self.memory = FakeMemory()
        self.client = None
    def ingest(self, p): self.G = nx.MultiDiGraph(); self.G.add_edge("a","b",relation="r")
    def stats(self): return {"nodes": (self.G.number_of_nodes() if self.G else 0),
                              "edges": (self.G.number_of_edges() if self.G else 0),
                              "communities": 0, "decisions": len(self.memory.stored)}
    def get_decisions(self): return []

class FakeCM:
    def __init__(self, cid, name):
        self.company_id = cid; self.company_name = name
        self.registry = {"doc1":{"category":"DOCUMENTS"}}
        self.knowledge_dg = FakeDG(); self.company_dg = FakeDG(); self.dg = self.company_dg
        from contextlib import contextmanager
        self._scoped_storage = contextmanager(lambda *a, **kw: (yield))
        self.company_subdir = "_test"
    def _graphs_payload(self): return []  # empty
    def ingest_knowledge(self, p): return "k_doc_1"
    def ingest_company(self, p):   return "c_doc_1"
    def stats(self): return {"company": self.company_name, "company_id": self.company_id,
                              "documents_ingested": len(self.registry),
                              "knowledge_graph": self.knowledge_dg.stats(),
                              "company_graph": self.company_dg.stats(),
                              "graph": self.company_dg.stats()}

class FakeHub:
    def __init__(self): self.companies = {}
    def add(self, cid, name): self.companies[cid] = FakeCM(cid, name); return self.companies[cid]
    def get_company(self, cid): return self.companies.get(cid)

def run(coro):  return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.get_event_loop().is_running() else asyncio.run(coro)

@t("invoke_tool dispatches list_companies")
def _h1():
    from decisiongraph.mcp_server import invoke_tool
    dg = FakeDG(); hub = FakeHub(); hub.add("acme","Acme Corp")
    r = run(invoke_tool("list_companies", {}, dg, hub, None))
    assert "companies" in r
    assert r["companies"][0]["id"] == "acme"
_h1()

@t("invoke_tool returns error for unknown tool")
def _h2():
    from decisiongraph.mcp_server import invoke_tool
    r = run(invoke_tool("nope", {}, FakeDG(), FakeHub(), None))
    assert "error" in r and "Unknown" in r["error"]
_h2()

@t("query_knowledge personal scope returns error if graph empty")
def _h3():
    from decisiongraph.mcp_server import invoke_tool
    dg = FakeDG()  # G is None
    r = run(invoke_tool("query_knowledge", {"question":"x"}, dg, FakeHub(), None))
    assert r.get("scope") is None or "error" in r
    assert "error" in r
_h3()

@t("query_knowledge with company_id targets that company")
def _h4():
    from decisiongraph.mcp_server import invoke_tool
    hub = FakeHub(); hub.add("acme", "Acme Corp")
    r = run(invoke_tool("query_knowledge", {"question":"x","company_id":"acme"}, FakeDG(), hub, None))
    assert r["scope"] == "company:acme"
_h4()

@t("query_knowledge with unknown company_id reports error")
def _h5():
    from decisiongraph.mcp_server import invoke_tool
    r = run(invoke_tool("query_knowledge", {"question":"x","company_id":"ghost"}, FakeDG(), FakeHub(), None))
    assert "error" in r and "ghost" in r["error"]
_h5()

@t("get_stats personal scope")
def _h6():
    from decisiongraph.mcp_server import invoke_tool
    r = run(invoke_tool("get_stats", {}, FakeDG(), FakeHub(), None))
    assert r["scope"] == "personal"
    assert "nodes" in r
_h6()

@t("get_stats company scope")
def _h7():
    from decisiongraph.mcp_server import invoke_tool
    hub = FakeHub(); hub.add("acme", "Acme Corp")
    r = run(invoke_tool("get_stats", {"company_id":"acme"}, FakeDG(), hub, None))
    assert r["scope"] == "company:acme"
    assert "knowledge_graph" in r and "company_graph" in r
_h7()

@t("store_decision personal")
def _h8():
    from decisiongraph.mcp_server import invoke_tool
    dg = FakeDG()
    r = run(invoke_tool("store_decision", {"question":"q","answer":"a","reasoning":"r"}, dg, FakeHub(), None))
    assert r["scope"] == "personal" and r["id"] == "d1"
_h8()

@t("store_decision company")
def _h9():
    from decisiongraph.mcp_server import invoke_tool
    hub = FakeHub(); hub.add("acme", "Acme")
    r = run(invoke_tool("store_decision", {"question":"q","answer":"a","reasoning":"r","company_id":"acme"}, FakeDG(), hub, None))
    assert r["scope"] == "company:acme"
_h9()

@t("ingest_document reports missing path cleanly")
def _h10():
    from decisiongraph.mcp_server import invoke_tool
    r = run(invoke_tool("ingest_document", {"path":"/does/not/exist.pdf"}, FakeDG(), FakeHub(), None))
    assert "error" in r and "does not exist" in r["error"]
_h10()

@t("ingest_document routes to knowledge_dg when target=knowledge")
def _h11():
    from decisiongraph.mcp_server import invoke_tool
    tmp = ROOT / "_tmp_mcp_test.txt"
    tmp.write_text("hi")
    try:
        hub = FakeHub(); hub.add("acme", "Acme")
        r = run(invoke_tool("ingest_document", {"path": str(tmp), "company_id":"acme","target":"knowledge"}, FakeDG(), hub, None))
        assert r["scope"] == "company:acme/knowledge"
        assert r["doc_id"] == "k_doc_1"
    finally:
        if tmp.exists(): tmp.unlink()
_h11()

@t("ingest_document routes to company_dg when target=company (default)")
def _h12():
    from decisiongraph.mcp_server import invoke_tool
    tmp = ROOT / "_tmp_mcp_test.txt"
    tmp.write_text("hi")
    try:
        hub = FakeHub(); hub.add("acme", "Acme")
        r = run(invoke_tool("ingest_document", {"path": str(tmp), "company_id":"acme"}, FakeDG(), hub, None))
        assert r["scope"] == "company:acme/company"
        assert r["doc_id"] == "c_doc_1"
    finally:
        if tmp.exists(): tmp.unlink()
_h12()

print(f"\n=== Results: {results['pass']} passed, {results['fail']} failed ===")
sys.exit(0 if results["fail"] == 0 else 1)
