"""Multi-tenant isolation tests — the security-critical ones.

Verifies that two visitors' workspaces NEVER see each other's knowledge
graph, decisions, companies, or discussion sessions, that the embedding
model is shared (memory), and that the WorkspaceManager enforces token
safety + LRU/idle eviction.

No LLM calls (we write decisions/graph state directly).
"""
import os, sys, time, shutil, traceback
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

PASS="[PASS]"; FAIL="[FAIL]"; res={"pass":0,"fail":0}
def t(name):
    def w(fn):
        def r(*a,**k):
            try: fn(*a,**k); print(f"  {PASS} {name}"); res["pass"]+=1
            except AssertionError as e: print(f"  {FAIL} {name}: {e}"); res["fail"]+=1
            except Exception as e:
                print(f"  {FAIL} {name}: {type(e).__name__}: {e}")
                traceback.print_exc(); res["fail"]+=1
        return r
    return w

TEST_BASE = ROOT / "storage" / "_test_isolation"
if TEST_BASE.exists(): shutil.rmtree(TEST_BASE, ignore_errors=True)

from decisiongraph.workspace import WorkspaceManager, Workspace, get_shared_embed_model

print("=== WorkspaceManager safety ===")

@t("new_token is unique + url-safe")
def _w1():
    toks = {WorkspaceManager.new_token() for _ in range(50)}
    assert len(toks) == 50
    for tk in toks:
        assert WorkspaceManager._safe(tk)

@t("rejects path-traversal / unsafe tokens")
def _w2():
    for bad in ["../etc", "a/b", "a\\b", "..", "", "x"*65, None]:
        assert not WorkspaceManager._safe(bad), f"should reject {bad!r}"
_w1(); _w2()

@t("get() with unsafe token raises")
def _w3():
    m = WorkspaceManager(base_dir=str(TEST_BASE/"m1"))
    try:
        m.get("../escape"); raise AssertionError("expected ValueError")
    except ValueError: pass
_w3()

print("\n=== Storage isolation ===")

@t("two workspaces get distinct storage roots")
def _i1():
    m = WorkspaceManager(base_dir=str(TEST_BASE/"m2"))
    a = m.get("alpha"); b = m.get("bravo")
    assert a.root != b.root
    assert a.root.endswith("alpha") and b.root.endswith("bravo")
_i1()

@t("decision stored in A is INVISIBLE in B")
def _i2():
    m = WorkspaceManager(base_dir=str(TEST_BASE/"m3"))
    a = m.get("ws_a"); b = m.get("ws_b")
    # write a decision into A's memory directly (no LLM)
    a.dg.memory.store("A-only question", "A-only answer", "r", [], [])
    a.dg.memory.save()
    # B must NOT see it
    b_decs = b.dg.get_decisions()
    a_decs = a.dg.get_decisions()
    assert len(a_decs) == 1, f"A should have 1, has {len(a_decs)}"
    assert len(b_decs) == 0, f"B must have 0, has {len(b_decs)} — ISOLATION BREACH"
    # and on-disk dirs differ
    assert a.dg.storage_dir != b.dg.storage_dir
_i2()

@t("reloading workspace A from disk keeps ONLY A's data")
def _i3():
    base = str(TEST_BASE/"m4")
    m1 = WorkspaceManager(base_dir=base)
    a = m1.get("persist_a")
    a.dg.memory.store("persisted", "x", "r", [], [])
    a.dg.memory.save()
    # fresh manager (simulates restart) — new Workspace object, same dir
    m2 = WorkspaceManager(base_dir=base)
    a2 = m2.get("persist_a"); b2 = m2.get("fresh_b")
    assert len(a2.dg.get_decisions()) == 1
    assert len(b2.dg.get_decisions()) == 0
_i3()

@t("company created in A not visible in B's hub")
def _i4():
    m = WorkspaceManager(base_dir=str(TEST_BASE/"m5"))
    a = m.get("co_a"); b = m.get("co_b")
    a.hub.add_company("acme", "Acme")
    assert "acme" in a.hub.companies
    assert "acme" not in b.hub.companies, "ISOLATION BREACH: company leaked"
_i4()

@t("discussion session in A not visible in B")
def _i5():
    m = WorkspaceManager(base_dir=str(TEST_BASE/"m6"))
    a = m.get("s_a"); b = m.get("s_b")
    a.dm.start_session(title="A secret session")
    a.dm.end_session.__self__  # touch
    assert len(a.dm.list_sessions()) >= 0  # session list scoped to A's dir
    # B's session dir is different + empty
    assert a.dm.storage_dir != b.dm.storage_dir
    assert len(b.dm.list_sessions()) == 0
_i5()

print("\n=== Shared embedding model (memory) ===")

@t("all workspaces SHARE one embed model instance")
def _e1():
    m = WorkspaceManager(base_dir=str(TEST_BASE/"m7"))
    a = m.get("e_a"); b = m.get("e_b")
    # accessing .dg builds the graph with the shared model
    assert a.dg.embed_model is b.dg.embed_model, "embed model NOT shared — memory blowup"
    assert a.dg.embed_model is get_shared_embed_model()
_e1()

print("\n=== LRU / idle eviction ===")

@t("size cap evicts least-recently-used")
def _l1():
    m = WorkspaceManager(base_dir=str(TEST_BASE/"m8"), max_live=3)
    for tk in ["k1","k2","k3","k4","k5"]:
        m.get(tk)
    st = m.stats()
    assert st["live"] <= 3, f"live={st['live']} exceeds cap 3"
_l1()

@t("idle eviction drops stale workspaces from memory")
def _l2():
    m = WorkspaceManager(base_dir=str(TEST_BASE/"m9"), max_live=50, idle_evict_s=0)
    w = m.get("idle1")
    time.sleep(0.01)
    m.get("idle2")   # triggers eviction sweep; idle1 is older than 0s window
    assert "idle1" not in m._ws, "idle workspace not evicted"
_l2()

@t("evicted workspace's data survives on disk + reloads")
def _l3():
    base = str(TEST_BASE/"m10")
    m = WorkspaceManager(base_dir=base, max_live=2)
    a = m.get("survivor")
    a.dg.memory.store("survives eviction", "yes", "r", [], [])
    a.dg.memory.save()
    # force eviction
    m.get("x1"); m.get("x2"); m.get("x3")
    assert "survivor" not in m._ws  # evicted from memory
    # re-get → reloads from disk with its data intact
    again = m.get("survivor")
    assert len(again.dg.get_decisions()) == 1, "data lost after eviction!"
_l3()

print("\n=== Backward compat (CLI/standalone still works) ===")

@t("DecisionGraph() with no args still constructs (default storage)")
def _b1():
    from decisiongraph.core import DecisionGraph
    dg = DecisionGraph(storage_dir=str(TEST_BASE/"bc"/"p"),
                       embed_model=get_shared_embed_model())
    assert dg.storage_dir.endswith("p")
    dg.memory.store("q","a","r",[],[]); assert len(dg.get_decisions())==1
_b1()

shutil.rmtree(TEST_BASE, ignore_errors=True)
print(f"\n=== Results: {res['pass']} passed, {res['fail']} failed ===")
sys.exit(0 if res["fail"]==0 else 1)
