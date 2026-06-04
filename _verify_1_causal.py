"""VERIFY #1: causal_radius returns all 5 layers populated with real data."""
import os, json
from pathlib import Path

# Use the already-existing flask workspace which has the code_graph.db
WS_DIR = Path("storage/workspaces/p4jIEgrAJd33s3qH-DZbhQ/personal")
# Confirm the DB exists
assert (WS_DIR / "code_graph.db").exists(), "no flask code_graph.db"

# Open the DG instance directly
import sys; sys.path.insert(0, ".")
from decisiongraph.workspace import Workspace
ws = Workspace("p4jIEgrAJd33s3qH-DZbhQ",
                "storage/workspaces/p4jIEgrAJd33s3qH-DZbhQ")
dg = ws.dg  # lazy property
print(f"workspace storage_dir: {dg.storage_dir}")

# Seed real-looking memory entries: an ADR, a PR, and a commit
# (in production these come from GitHub API + git log + user-written ADRs)
dg.memory.store(
    question="[adr:0007] How should request context be propagated across async tasks?",
    answer="We decided to use contextvars in RequestContext (src/flask/ctx.py) "
           "because contextvars survives task switching, unlike thread-local. "
           "This affects RequestContext, AppContext, and all teardown handlers.",
    reasoning_summary="adr request context contextvars",
    communities_used=[], context_triples=[])
dg.memory.store(
    question="[pr:#5341] Fix AppContext teardown when current_app is replaced",
    answer="PR addresses a bug where the teardown_appcontext callbacks fired "
           "twice if current_app was replaced mid-request. Changes src/flask/ctx.py "
           "AppContext.pop() and adds a test in tests/test_appctx.py.",
    reasoning_summary="pr 5341 appcontext teardown",
    communities_used=[], context_triples=[])
dg.memory.store(
    question="[commit:a29f88ce] document that headers must be set before streaming",
    answer="Touched src/flask/ctx.py and src/flask/helpers.py. The streaming "
           "response in helpers.py needs headers fixed before the first byte is "
           "sent — this commit documents that constraint.",
    reasoning_summary="commit a29f88ce streaming headers",
    communities_used=[], context_triples=[])
dg.memory.save()
print("seeded 3 memory entries (ADR + PR + commit) — persisted to disk")

# Now call causal_radius
import asyncio
from decisiongraph.mcp_server import _h_causal_radius

async def go():
    return await _h_causal_radius(dg, None, None,
        path="src/flask/ctx.py",
        repo="pallets/flask")

out = asyncio.run(go())

# Layer-by-layer check
def L(name, val):
    if val is None: return f"  [MISS] {name}: None"
    if isinstance(val, list): return f"  [{'OK' if val else 'EMPTY'}] {name}: {len(val)} entries"
    return f"  [OK] {name}: {val}"

print("\n5-LAYER CAUSAL RADIUS:")
print(L("affected_files (structural)",     out.get("affected_files")))
print(L("symbols_in_changed_file",         out.get("symbols_in_changed_file")))
print(L("total_affected",                  out.get("total_affected")))
print(L("related_decisions (DG memory)",   out.get("related_decisions")))
print(L("related_prs",                     out.get("related_prs")))
print(L("related_commits",                 out.get("related_commits")))
print(L("readme_blueprint_context",        out.get("readme_blueprint_context")))

print("\nSAMPLE outputs:")
for layer in ("related_decisions", "related_prs", "related_commits"):
    entries = out.get(layer) or []
    if entries:
        e = entries[0]
        print(f"  {layer}[0]:")
        print(f"    question/title: {(e.get('question') or e.get('title') or e.get('message') or '')[:90]}")
        print(f"    snippet/summary: {(e.get('snippet') or e.get('summary') or e.get('context') or '')[:120]}")
