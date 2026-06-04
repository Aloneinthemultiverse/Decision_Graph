"""REAL coding-test: side-by-side what an AI agent sees with/without our graph.

Scenario: "Add `mark_dirty()` method to Flask's AppContext to signal context
modified state. What do I need to know before this change?"

Without our tools: only static file read of ctx.py
With our tools:    find_callers + blast_radius + causal_radius + rationales
                    + topology + related decisions
"""
import asyncio, json
from decisiongraph import code_graph as cg
from decisiongraph.workspace import Workspace
from decisiongraph.mcp_server import _h_causal_radius

DB   = (r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ"
        r"\personal\code_graph.db")
REPO = "pallets/flask"

print("=" * 72)
print("SCENARIO: 'Add mark_dirty() method to AppContext. What should I know?'")
print("=" * 72)

print("\n" + "-" * 72)
print("[A] WITHOUT our graph — what a vanilla agent sees")
print("-" * 72)
# Agent does what every coding-agent does: reads the file.
import re
src = open(r"C:\Users\SUJITN~1\AppData\Local\Temp\flask-bench\src\flask\ctx.py",
           encoding="utf-8").read()
print(f"  reads ctx.py ({len(src)} chars)")
# count methods on AppContext
m = re.search(r"class AppContext.*?(?=\nclass |\Z)", src, re.DOTALL)
if m:
    methods = re.findall(r"def (\w+)", m.group(0))
    print(f"  finds AppContext class with {len(methods)} methods: {methods}")
print("  agent's typical answer: 'add the method between push() and pop().")
print("                            you may want a flag _dirty = False in __init__.")
print("                            consider thread safety.'")
print("  -> that's it. The agent has no idea about callers, no idea about ADRs,")
print("    no warning about god-nodes, no clue who'll break.")

print("\n" + "-" * 72)
print("[B] WITH our graph — what the agent gets via MCP tools")
print("-" * 72)

# Tool 1: find_callers of AppContext methods
print("\n  TOOL 1: find_callers('AppContext') -> who uses this class today?")
callers = cg.find_callers(DB, "AppContext", repo=REPO, limit=10)
print(f"    {len(callers)} callers (top 5):")
for c in callers[:5]:
    print(f"      {c['path']:42}  {c['caller']:30}  ({c['bucket']})")

# Tool 2: find_callers of specific AppContext methods that are most-affected siblings
print("\n  TOOL 2: find_callers('push') and ('pop') — sibling methods")
for nm in ("push", "pop"):
    rs = cg.find_callers(DB, nm, repo=REPO, limit=5)
    print(f"    {nm}: {len(rs)} callers (across all classes with .push/.pop)")

# Tool 3: blast_radius of ctx.py
print("\n  TOOL 3: blast_radius('src/flask/ctx.py')")
br = cg.blast_radius(DB, "src/flask/ctx.py", repo=REPO, max_depth=3, strict=True)
print(f"    {br['total_affected']} files might be affected by changes here")
print(f"    hops: { {h: len(v) for h, v in br['callers_by_hop'].items()} }")
print(f"    sample affected: {br['affected_files'][:4]}")

# Tool 4: rationales near AppContext
print("\n  TOOL 4: rationales_in_file('src/flask/ctx.py')")
rats = cg.rationales_in_file(DB, "src/flask/ctx.py", repo=REPO)
print(f"    {len(rats)} inline rationales found:")
for r in rats[:5]:
    print(f"      [{r['tag']}] line {r['line']}: {r['text'][:70]}")

# Tool 5: causal_radius (full 5-layer picture)
print("\n  TOOL 5: causal_radius — fused structural + decision + PR + commit + README")
ws = Workspace("p4jIEgrAJd33s3qH-DZbhQ",
               "storage/workspaces/p4jIEgrAJd33s3qH-DZbhQ")
out = asyncio.run(_h_causal_radius(ws.dg, None, None,
    path="src/flask/ctx.py", repo=REPO))
print(f"    affected_files:          {out['total_affected']}")
print(f"    related_decisions:       {out['total_related_decisions']} ADRs touching ctx.py")
print(f"    related_prs:             {len(out.get('related_prs') or [])} historical PRs")
print(f"    related_commits:         {len(out.get('related_commits') or [])} commits")
print(f"    readme_blueprint_ctx:    {len(out.get('readme_blueprint_context') or [])} doc snippets")
# show one related decision
for d in (out.get("related_decisions") or [])[:1]:
    print(f"    -> relevant ADR: \"{d['question'][:80]}\"")
    print(f"      excerpt: \"{d['snippet'][:120]}\"")

# Tool 6: triage_pr — simulate what triage would say IF this were a PR
print("\n  TOOL 6: triage_pr — if you submitted this as a PR right now")
triage = cg.triage_pr(DB, ["src/flask/ctx.py"], repo=REPO, max_hops=3)
print(f"    risk_score:        {triage['risk_score']} ({triage['risk_tier']})")
print(f"    god_nodes_touched: {triage['god_nodes_touched']}")
print(f"    merge_order_hint:  {triage['merge_order_hint']}")
if triage['rationale_warnings']:
    print(f"    rationale warnings: {len(triage['rationale_warnings'])} HACK/SAFETY/BUG")

# Tool 7: topology — is AppContext a god-node?
print("\n  TOOL 7: topology — is this code central or peripheral?")
topo = cg.analyze_topology(DB, repo=REPO, top_god=20, top_surprise=0)
ctx_god_hits = [g for g in topo["god_nodes"]
                if "ctx" in g["path"].lower() or "AppContext" in g["qualified_name"]]
if ctx_god_hits:
    print(f"    AppContext-related symbols in top-20 god-nodes:")
    for g in ctx_god_hits[:5]:
        print(f"      {g['qualified_name']:40}  {g['caller_count']} callers")
else:
    print(f"    AppContext NOT in top-20 god-nodes (peripheral)")

print("\n" + "=" * 72)
print("VERDICT — what the with-graph agent now knows that vanilla doesn't:")
print("=" * 72)
print(f"""
  - {len(callers)} concrete files that instantiate AppContext
  - {br['total_affected']} files in the structural blast radius
  - {len(rats)} captured design rationales in ctx.py
  - {out['total_related_decisions']} prior ADRs touching ctx.py
  - {len(out.get('related_prs') or [])} historical PRs to read first
  - Risk score for this change: {triage['risk_score']} ({triage['risk_tier']})
  - Merge-order guidance: "{triage['merge_order_hint'][:70]}"

  None of this is in the source file. A code-only agent CANNOT see it.
""")
