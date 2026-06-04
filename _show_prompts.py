"""Show the EXACT prompts the LLM would receive in both runs.
Same scenario; no LLM call (gateway is down). The graph signals are real."""
import json, sqlite3
from pathlib import Path
from decisiongraph import code_graph as cg

FASTAPI  = Path(r"C:\Users\SUJITN~1\AppData\Local\Temp\fastapi-bench")
DB       = "storage/bench_graphs/fastapi.db"
REPO     = "tiangolo/fastapi"
TARGET   = "fastapi/dependencies/utils.py"
QUESTION = ("I want to add a new dependency-injection mechanism that takes "
            "priority over the existing Depends() — what do I need to know "
            "before touching the dependency-resolution code in "
            f"{TARGET}?")

src = (FASTAPI / TARGET).read_text(encoding="utf-8", errors="replace")[:8000]

# ── Vanilla prompt ────────────────────────────────────────────────────
vanilla = (
    f"You are a code reviewer. The file `{TARGET}` looks like this:\n\n"
    f"```python\n{src}\n```\n\n"
    f"QUESTION: {QUESTION}\n\n"
    f"Respond covering risks, prior decisions, files to update, "
    f"and merge-order guidance.")

# ── Collect MCP signals ───────────────────────────────────────────────
br      = cg.blast_radius(DB, TARGET, repo=REPO, max_depth=3, strict=True)
callers = cg.find_callers(DB, "solve_dependencies", repo=REPO, limit=10)
rats    = cg.rationales_in_file(DB, TARGET, repo=REPO)
topo    = cg.analyze_topology(DB, repo=REPO, top_god=5, top_surprise=0)
triage  = cg.triage_pr(DB, [TARGET], repo=REPO, max_hops=3)
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
all_warning_rats = [dict(r) for r in conn.execute(
    "SELECT tag, path, line, text FROM rationales "
    "WHERE tag IN ('HACK','SAFETY','BUG','FIXME','WARNING','DEPRECATED') "
    "LIMIT 10").fetchall()]
conn.close()

ctx = {
    "blast_radius": {"total_affected": br["total_affected"],
                      "sample": br["affected_files"][:10]},
    "callers_of_solve_dependencies": [
        {"caller": c["caller"], "path": c["path"], "bucket": c["bucket"]}
        for c in callers[:8]],
    "rationales_in_target_file": rats[:5],
    "fastapi_warning_rationales_anywhere": all_warning_rats,
    "god_nodes_top5": [
        {"name": g["qualified_name"], "callers": g["caller_count"],
          "path": g["path"]} for g in topo["god_nodes"][:5]],
    "pr_triage_if_submitted_now": {
        "risk_score":         triage["risk_score"],
        "risk_tier":          triage["risk_tier"],
        "god_nodes_touched":  triage["god_nodes_touched"],
        "merge_order_hint":   triage["merge_order_hint"],
        "rationale_warnings": [{"tag": w["tag"], "text": w["text"][:80]}
                                for w in triage["rationale_warnings"][:5]],
    },
}

with_graph = (
    f"You are a code reviewer. The file `{TARGET}` looks like this:\n\n"
    f"```python\n{src}\n```\n\n"
    f"You ALSO have these GRAPH SIGNALS from the DecisionGraph code "
    f"intelligence layer (these come from a structural + semantic index "
    f"of the entire codebase, not the file above):\n\n"
    f"```json\n{json.dumps(ctx, indent=2)}\n```\n\n"
    f"QUESTION: {QUESTION}\n\n"
    f"Respond covering risks, prior decisions, files to update, and "
    f"merge-order guidance. When you cite a fact, anchor it to a graph "
    f"signal where possible.")

# ── Output ────────────────────────────────────────────────────────────
print("=" * 78)
print(f"VANILLA prompt size:    {len(vanilla):>6} chars")
print(f"WITH-GRAPH prompt size: {len(with_graph):>6} chars  "
      f"(+{len(with_graph) - len(vanilla)} extra)")
print("=" * 78)
print(f"\n--- extra context the with-graph agent sees ---\n")
print(json.dumps(ctx, indent=2))

Path("with_graph_prompt.txt").write_text(with_graph, encoding="utf-8")
Path("vanilla_prompt.txt"   ).write_text(vanilla,    encoding="utf-8")
print("\nFull prompts saved to vanilla_prompt.txt and with_graph_prompt.txt")
print("(re-run with the LLM once your ngrok gateway is back up.)")
