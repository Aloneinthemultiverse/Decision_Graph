"""Call every one of the 27 registered MCP tools at least once."""
import asyncio, traceback, sys
import os
from dotenv import load_dotenv
load_dotenv()
# Override to local Antigravity gateway (the ngrok URL in .env is offline)
os.environ["LLM_BASE_URL"] = "http://localhost:8080"
os.environ["LLM_API_KEY"] = os.environ.get("LLM_API_KEY") or "test"
from decisiongraph.mcp_server import TOOL_HANDLERS, invoke_tool
from decisiongraph.core import DecisionGraph
from decisiongraph.company import EnterpriseHub
from decisiongraph.discussion import DiscussionManager

# Use an existing populated workspace for richer signals
os.environ["DG_CODE_GRAPH_DB"] = r"C:\Users\SUJITN~1\AppData\Local\Temp\loop_flask\.dg_code_graph.db"

dg  = DecisionGraph(storage_dir="storage/workspaces/p4jIEgrAJd33s3qH-DZbhQ/personal")
# Force the knowledge-graph load so query_knowledge can find content
dg._ensure_graph_loaded()
hub = EnterpriseHub()
dm  = DiscussionManager()

# Reasonable argument fixtures per tool
ARGS = {
    "list_companies":       {},
    "query_knowledge":      {"question": "what is in the graph?"},
    "get_past_decisions":   {"context": "ctx.py"},
    "store_decision":       {"question": "[adr:smoke-test] tool exercise",
                              "answer":   "ran during 27-tool smoke test",
                              "reasoning": "smoke test"},
    "ingest_document":      {"path": ".bench/test.pdf"},
    "ingest_url":           {"url": "https://www.example.com"},
    "ingest_github":        {"repo_url": "https://github.com/pallets/flask"},
    "get_stats":            {},
    "recall":               {"entity": "Flask"},
    "recent_code_edits":    {"limit": 5},
    "track_code_edit":      {"file_path": "src/flask/ctx.py",
                              "before": "def push(self): pass",
                              "after":  "def push(self): self.app.do_setup()",
                              "agent_id": "smoke-test"},
    "track_decision":       {"question": "smoke test track",
                              "answer":   "ok",
                              "reasoning": "test"},
    "review_communities":   {},
    "set_active_company":   {"company_id": "nebula"},
    "compile_topic":        {"community_id": 0},
    "supersede_decision":   {"old_decision_id": "nope", "new_decision_id": "nope"},
    "agent_react":          {"goal": "summarise"},
    "agent_run":            {"agent_id": "knowledge-summarizer",
                              "goal":     "summarise"},
    "list_agents":          {},
    "scope_check":          {"agent_id": "x", "action": "read", "target": "y"},
    "agent_run_sandboxed":  {"agent_id": "x", "goal": "noop"},
    "find_callers":         {"name": "Flask"},
    "find_callees":         {"name": "Flask"},
    "blast_radius":         {"path": "src/flask/ctx.py", "max_depth": 1},
    "find_call_path":       {"src": "Flask", "dst": "Scaffold"},
    "causal_radius":        {"path": "src/flask/ctx.py"},
    "code_graph_stats":     {},
    "rationales":           {"path": "src/flask/ctx.py"},
    "topology":             {"top_god": 3, "top_surprise": 0},
    "suggested_questions":  {},
    "triage_pr":            {"changed_files": ["src/flask/ctx.py"]},
    "export_graph_html":    {"out_path": ".bench/_27test.html",
                              "max_nodes": 100},
    "export_graph_cypher":  {"out_path": ".bench/_27test.cypher"},
    "federated_callers":    {"symbol": "Flask",
                              "workspace_roots": ["storage/bench_graphs"]},
    "federated_topology":   {"workspace_roots": ["storage/bench_graphs"],
                              "top_god": 3},
    "federated_rationale_search":
                            {"query": "TODO", "tag": None,
                              "workspace_roots": ["storage/bench_graphs"]},
    "get_onboarding_brief": {},
}

results = {"pass": [], "skip": [], "fail": []}

async def call(name, args):
    return await invoke_tool(name, args, dg, hub, dm)

for name in sorted(TOOL_HANDLERS.keys()):
    args = ARGS.get(name)
    if args is None:
        results["skip"].append((name, "no fixture defined"))
        continue
    try:
        r = asyncio.run(call(name, args))
        if isinstance(r, dict) and "error" in r:
            results["fail"].append((name, str(r["error"])[:100]))
        else:
            keys = list(r.keys())[:3] if isinstance(r, dict) else type(r).__name__
            results["pass"].append((name, keys))
    except Exception as e:
        results["fail"].append((name, f"{type(e).__name__}: {e}"[:100]))

print(f"\n=== RESULTS ({len(TOOL_HANDLERS)} tools total) ===")
print(f"PASS: {len(results['pass'])}")
for n, k in results["pass"]:
    print(f"   [PASS] {n:<35} -> {k}")
print(f"\nFAIL: {len(results['fail'])}")
for n, err in results["fail"]:
    print(f"   [FAIL] {n:<35} -> {err}")
print(f"\nSKIP (no fixture): {len(results['skip'])}")
for n, _ in results["skip"]:
    print(f"   [SKIP] {n}")
