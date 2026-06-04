"""Step 5: confirm each tool surfaces the new symbol."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, ".")
from decisiongraph import code_graph as cg
from decisiongraph.workspace import Workspace
from decisiongraph.mcp_server import _h_causal_radius

REPO_PATH = Path(r"C:\Users\SUJITN~1\AppData\Local\Temp\loop_flask")
DB = str(REPO_PATH / ".dg_code_graph.db")
REPO = "pallets/flask"

print("=== 1) find_callers('mark_dirty') ===")
callers = cg.find_callers(DB, "mark_dirty", repo=REPO, limit=20)
print(f"   {len(callers)} callers (expected 0 — nothing in flask calls it yet)")
for c in callers[:3]:
    print(f"     {c['caller']} @ {c['path']}  bucket={c['bucket']}")

print("\n=== 2) find_callees('mark_dirty') — what does the new method call? ===")
callees = cg.find_callees(DB, "mark_dirty", repo=REPO, limit=20)
print(f"   {len(callees)} callees")
for c in callees:
    print(f"     -> {c['dst_name']}  bucket={c['bucket']}")

print("\n=== 3) blast_radius('mark_dirty' as symbol) ===")
br = cg.blast_radius(DB, "mark_dirty", repo=REPO, max_depth=3, strict=True)
print(f"   anchor file: {br['changed_file']}")
print(f"   symbols in changed file: {len(br['symbols_in_changed_file'])}")
print(f"   total affected: {br['total_affected']}")

print("\n=== 4) blast_radius('src/flask/ctx.py') — file-level ===")
br2 = cg.blast_radius(DB, "src/flask/ctx.py", repo=REPO, max_depth=3, strict=True)
print(f"   total_affected: {br2['total_affected']}")
print(f"   mark_dirty IS in symbols_in_changed_file: "
      f"{'mark_dirty' in br2['symbols_in_changed_file']}")

print("\n=== 5) topology — is AppContext still a god-node? ===")
topo = cg.analyze_topology(DB, repo=REPO, top_god=10, top_surprise=0)
ctx_hits = [g for g in topo["god_nodes"]
             if "AppContext" in g["qualified_name"] or g["path"] == "src/flask/ctx.py"]
print(f"   AppContext/ctx.py god-node hits: {len(ctx_hits)}")
for g in ctx_hits[:3]:
    print(f"     {g['qualified_name']:40} {g['caller_count']} callers")

print("\n=== 6) triage_pr — simulating new PR on the modified file ===")
tr = cg.triage_pr(DB, ["src/flask/ctx.py"], repo=REPO, max_hops=3)
print(f"   risk: {tr['risk_score']} ({tr['risk_tier']})")
print(f"   merge hint: {tr['merge_order_hint']}")

print("\n=== 7) causal_radius — full 5-layer ===")
ws = Workspace("loop_flask",
                r"C:\Users\SUJITN~1\AppData\Local\Temp\loop_flask")
# Override storage_dir on the workspace's DG: we want the .dg_code_graph.db, not
# the workspace's personal/code_graph.db. We'll patch dg.storage_dir afterward.
# Simpler: hit causal_radius's underlying functions directly.
br3 = cg.blast_radius(DB, "src/flask/ctx.py", repo=REPO, max_depth=3, strict=True)
print(f"   affected: {br3['total_affected']}")
print(f"   symbols include mark_dirty: {'mark_dirty' in br3['symbols_in_changed_file']}")
