"""Tests for the 9 gbrain-inspired features. No LLM, no server."""
import os, sys, time, shutil, traceback, tempfile
from pathlib import Path
ROOT = Path(__file__).parent; sys.path.insert(0, str(ROOT))

PASS="[PASS]"; FAIL="[FAIL]"; res={"pass":0,"fail":0}
def t(n):
    def w(fn):
        def r(*a,**k):
            try: fn(*a,**k); print(f"  {PASS} {n}"); res["pass"]+=1
            except AssertionError as e: print(f"  {FAIL} {n}: {e}"); res["fail"]+=1
            except Exception as e:
                print(f"  {FAIL} {n}: {type(e).__name__}: {e}"); traceback.print_exc(); res["fail"]+=1
        return r
    return w

TB = ROOT/"storage"/"_test_gbrain"
if TB.exists(): shutil.rmtree(TB, ignore_errors=True)
from decisiongraph.decisions import DecisionMemory

def mem(name):
    d = str(TB/name); os.makedirs(d, exist_ok=True)
    return DecisionMemory(storage_dir=d)

class FakeClient:
    class _M:
        @staticmethod
        def create(**kw):
            class B: type="text"; text="Compiled understanding: prefer X over Y."
            class R: content=[B()]
            return R()
    messages=_M()

print("=== #5 self-wiring links ===")
@t("decisions sharing a community auto-link (zero LLM)")
def _a1():
    m=mem("a1")
    d1=m.store("q1","a1","r",[7],[])
    d2=m.store("q2","a2","r",[7],[])
    d3=m.store("q3","a3","r",[99],[])
    assert m.graph.has_edge(d1,d2) or m.graph.has_edge(d2,d1), "shared-community link missing"
    assert not (m.graph.has_edge(d1,d3) or m.graph.has_edge(d3,d1)), "unrelated wrongly linked"
    e = m.graph.get_edge_data(d2,d1) or m.graph.get_edge_data(d1,d2)
    assert e.get("relation")=="shares_community"
_a1()
@t("relink_all is idempotent (no dup edges)")
def _a2():
    m=mem("a2"); m.store("x","a","r",[1],[]); m.store("y","b","r",[1],[])
    before=m.graph.number_of_edges(); m.relink_all(); after=m.graph.number_of_edges()
    assert before==after, f"relink added dup edges {before}->{after}"
_a2()

print("\n=== #1 compiled truth + timeline ===")
@t("timeline is chronological + immutable evidence")
def _c1():
    m=mem("c1")
    m.store("first","a","r",[5],[]); time.sleep(0.01); m.store("second","a","r",[5],[])
    tl=m.get_timeline(5)
    assert len(tl)==2 and tl[0]["question"]=="first" and tl[1]["question"]=="second"
_c1()
@t("compile_topic writes compiled truth, get_compiled returns it")
def _c2():
    m=mem("c2"); m.store("should we ship?","yes","r",[3],[])
    txt=m.compile_topic(3, FakeClient(), "shipping")
    assert "Compiled understanding" in txt
    g=m.get_compiled(3)
    assert g["compiled_truth"]==txt and g["evidence_count"]==1 and len(g["timeline"])==1
_c2()
@t("compiled truth persists across save/load")
def _c3():
    d=str(TB/"c3"); os.makedirs(d,exist_ok=True)
    m=DecisionMemory(storage_dir=d); m.store("q","a","r",[2],[])
    m.compile_topic(2, FakeClient()); m.save()
    m2=DecisionMemory(storage_dir=d); m2.load()
    assert m2.get_compiled(2)["compiled_truth"], "compiled truth lost on reload"
_c3()
@t("legacy raw-graph pickle still loads (backward compat)")
def _c4():
    import pickle, networkx as nx
    d=str(TB/"c4"); os.makedirs(d,exist_ok=True)
    g=nx.DiGraph()
    with open(os.path.join(d,"decision_graph.pkl"),"wb") as f: pickle.dump(g,f)
    m=DecisionMemory(storage_dir=d); m.load()  # must not raise
    assert m.graph.number_of_nodes()==0
_c4()

print("\n=== #2 hybrid retrieval + RRF ===")
@t("keyword_search ranks token-overlap triples")
def _k1():
    import networkx as nx
    from decisiongraph.query import keyword_search
    G=nx.MultiDiGraph()
    G.add_edge("pricing strategy","margin",relation="affects")
    G.add_edge("hiring plan","budget",relation="impacts")
    r=keyword_search("what is our pricing strategy",G)
    assert r and "pricing" in r[0].lower()
_k1()
@t("rrf_fuse merges rankings, consensus wins")
def _k2():
    from decisiongraph.query import rrf_fuse
    a=["x","y","z"]; b=["y","x","w"]
    fused=rrf_fuse(a,b)
    assert fused[0] in ("x","y") and set(fused)>= {"x","y","z","w"}
_k2()

print("\n=== #8 intent routing ===")
@t("classify_intent buckets correctly")
def _i1():
    from decisiongraph.query import classify_intent, intent_to_mode
    assert classify_intent("what happened with the incident")=="event"
    assert classify_intent("who works at acme")=="entity"
    assert classify_intent("show the timeline of pricing changes")=="temporal"
    assert classify_intent("explain our strategy")=="general"
    assert intent_to_mode("entity")=="session"
    assert intent_to_mode("temporal")=="deep"
_i1()

print("\n=== #4 Dream Cycle ===")
@t("run_dream_cycle returns phased report + persists")
def _d1():
    from decisiongraph.workspace import WorkspaceManager
    from decisiongraph.dream import run_dream_cycle
    wm=WorkspaceManager(base_dir=str(TB/"dwm"))
    ws=wm.get("dreamer")
    ws.dg.memory.store("dup decision","x","r",[4],[])
    ws.dg.memory.store("dup decision","x","r",[4],[])  # near-dup
    rep=run_dream_cycle(ws, compile_topics=False, dedupe=True)
    assert "phases" in rep and "decay" in rep["phases"] and "relink" in rep["phases"]
    assert "dedupe" in rep["phases"]
    assert rep["duration_s"] >= 0
_d1()

print("\n=== #6 durable job queue ===")
@t("enqueue → worker runs handler → done with result")
def _j1():
    from decisiongraph.jobs import JobQueue
    q=JobQueue(str(TB/"jq1"/"jobs.db"))
    q.register("echo", lambda p:{"echoed":p.get("v")})
    q.start()
    jid=q.enqueue("echo",{"v":42},workspace="w1")
    for _ in range(50):
        time.sleep(0.1)
        j=q.get(jid)
        if j and j["status"] in ("done","failed"): break
    q.stop()
    assert j["status"]=="done", f"status={j['status']} err={j.get('error')}"
    assert j["result"]=={"echoed":42}
_j1()
@t("interrupted 'running' jobs are requeued on restart (replay-safe)")
def _j2():
    import sqlite3
    from decisiongraph.jobs import JobQueue
    db=str(TB/"jq2"/"jobs.db")
    q=JobQueue(db)  # creates schema
    from datetime import datetime
    with sqlite3.connect(db) as c:
        now=datetime.now().isoformat()
        c.execute("INSERT INTO jobs(type,payload,status,created_at,updated_at)"
                  " VALUES('echo','{}','running',?,?)",(now,now))
    q.register("echo", lambda p:{"ok":True})
    q.start()
    time.sleep(1.5)
    q.stop()
    rows=q.list(limit=10)
    assert rows and rows[0]["status"]=="done", f"interrupted job not replayed: {rows}"
_j2()
@t("unknown job type fails cleanly (no crash)")
def _j3():
    from decisiongraph.jobs import JobQueue
    q=JobQueue(str(TB/"jq3"/"jobs.db")); q.start()
    jid=q.enqueue("nope",{})
    for _ in range(40):
        time.sleep(0.1); j=q.get(jid)
        if j and j["status"]=="failed": break
    q.stop()
    assert j["status"]=="failed" and "no handler" in (j["error"] or "")
_j3()

print("\n=== #9 eval capture + replay ===")
@t("capture appends redacted records, load reads them")
def _e1():
    from decisiongraph import evals_capture as ec
    d=str(TB/"ev1"); os.makedirs(d,exist_ok=True)
    ec.capture(d, question="email me at bob@x.com about 12345678 budget",
               mode="deep", intent="event", communities=3)
    recs=ec.load(d)
    assert len(recs)==1
    assert "<email>" in recs[0]["q"] and "bob@x.com" not in recs[0]["q"]
    assert "<num>" in recs[0]["q"]
_e1()
@t("replay with no graph returns graceful note")
def _e2():
    from decisiongraph import evals_capture as ec
    from decisiongraph.core import DecisionGraph
    from decisiongraph.workspace import get_shared_embed_model
    d=str(TB/"ev2"); os.makedirs(d,exist_ok=True)
    ec.capture(d, question="anything", mode="deep")
    dg=DecisionGraph(storage_dir=d, embed_model=get_shared_embed_model())
    r=ec.replay(dg, d)
    assert r["captured"]==1 and r["retrieval_rate"] is None
_e2()

print("\n=== #3 recall MCP tool ===")
@t("recall tool returns active facts mentioning entity, no LLM")
def _r1():
    import asyncio
    from decisiongraph.mcp_server import invoke_tool, TOOL_DEFS
    from decisiongraph.core import DecisionGraph
    from decisiongraph.workspace import get_shared_embed_model
    assert "recall" in {t.name for t in TOOL_DEFS}
    d=str(TB/"rc1"); os.makedirs(d,exist_ok=True)
    dg=DecisionGraph(storage_dir=d, embed_model=get_shared_embed_model())
    dg.memory.store("Acme pricing decision","raise to $499","r",[1],[])
    dg.memory.store("unrelated thing","blah","r",[2],[])
    out=asyncio.get_event_loop().run_until_complete(
        invoke_tool("recall",{"entity":"acme"},dg,None,None))
    assert out["facts"] and "Acme" in out["facts"][0]["question"]
    assert len(out["facts"])==1, "matched unrelated decision"
_r1()

shutil.rmtree(TB, ignore_errors=True)
print(f"\n=== Results: {res['pass']} passed, {res['fail']} failed ===")
sys.exit(0 if res["fail"]==0 else 1)
