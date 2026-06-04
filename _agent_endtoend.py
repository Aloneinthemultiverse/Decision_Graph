"""End-to-end agent demo: pipe MCP tool output into a real LLM call.

Runs the same question twice:
  RUN 1 (vanilla):    LLM only sees the raw source file
  RUN 2 (with graph): LLM sees MCP tool output (causal_radius + rationales + triage)

Uses fastapi as the codebase (has 20 real rationales, big polymorphic
codebase — the failure case from earlier benchmarks).

Question: 'I want to add a new dependency-injection mechanism that takes
priority over the existing Depends() — what do I need to know before
touching the dependency-resolution code?'
"""
import os, json, asyncio
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()    # loads .env into os.environ; config.py reads from env
# Re-import config after load so it picks up the env values
import importlib
from decisiongraph import config as dg_config_mod
importlib.reload(dg_config_mod)
from decisiongraph import code_graph as cg
from decisiongraph.workspace import Workspace
from decisiongraph.mcp_server import _h_causal_radius

import anthropic
dg_config = dg_config_mod

FASTAPI = Path(r"C:\Users\SUJITN~1\AppData\Local\Temp\fastapi-bench")
DB      = "storage/bench_graphs/fastapi.db"
REPO    = "tiangolo/fastapi"
QUESTION = ("I want to add a new dependency-injection mechanism that takes "
            "priority over the existing Depends() — what do I need to know "
            "before touching the dependency-resolution code in "
            "fastapi/dependencies/utils.py?")

# Pick the file we'd be touching
TARGET_FILE = "fastapi/dependencies/utils.py"

# ── LLM setup ─────────────────────────────────────────────────────────
client = anthropic.Anthropic(
    api_key=dg_config.LLM_API_KEY or "test",
    base_url="http://localhost:8080",   # Antigravity local gateway
    default_headers={"ngrok-skip-browser-warning": "true"},
)
MODEL = "gemini-3-flash"
def call_llm(system: str, user: str) -> str:
    # Gemini-3-flash uses LOTS of tokens on internal "thinking" blocks
    # before emitting text. 8k gives enough headroom for both.
    r = client.messages.create(
        model=MODEL, max_tokens=8000, system=system,
        messages=[{"role": "user", "content": user}],
    )
    out_text = []
    out_thinking_chars = 0
    for b in r.content:
        t = getattr(b, "type", "")
        if t == "text":
            out_text.append(getattr(b, "text", ""))
        elif t == "thinking":
            out_thinking_chars += len(getattr(b, "thinking", "") or "")
    answer = "\n".join(s for s in out_text if s).strip()
    print(f"  [LLM] in={getattr(r.usage,'input_tokens','?')} "
          f"out={getattr(r.usage,'output_tokens','?')} "
          f"stop={getattr(r,'stop_reason','?')} "
          f"thinking_chars={out_thinking_chars} text_chars={len(answer)}")
    return answer

# ── RUN 1 — vanilla agent (just file content) ─────────────────────────
print("=" * 78)
print("RUN 1 — VANILLA agent (only sees the raw source file)")
print("=" * 78)

target_path = FASTAPI / TARGET_FILE
src = target_path.read_text(encoding="utf-8", errors="replace")[:18_000]
vanilla_prompt = (
    f"You are a code reviewer. The file `{TARGET_FILE}` looks like this:\n\n"
    f"```python\n{src}\n```\n\n"
    f"QUESTION: {QUESTION}\n\n"
    f"Respond in MAX 12 bullet points covering risks, prior decisions, "
    f"files to update, and merge-order guidance. If you don't know any "
    f"of those, say so explicitly.")

vanilla_answer = call_llm(
    "You are a senior engineer reviewing a proposed change.",
    vanilla_prompt)
print(vanilla_answer)

# ── Gather MCP tool output for run 2 ──────────────────────────────────
print("\n" + "=" * 78)
print("Collecting tool outputs for run 2...")
print("=" * 78)

# 1. blast_radius / find_callers
br      = cg.blast_radius(DB, TARGET_FILE, repo=REPO, max_depth=3, strict=True)
callers = cg.find_callers(DB, "solve_dependencies", repo=REPO, limit=10)
rats    = cg.rationales_in_file(DB, TARGET_FILE, repo=REPO)
topo    = cg.analyze_topology(DB, repo=REPO, top_god=8, top_surprise=0)
triage  = cg.triage_pr(DB, [TARGET_FILE], repo=REPO, max_hops=3)
# all rationales in fastapi (not just this file) - to show 20 real ones surface
all_rats = []
import sqlite3
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
for r in conn.execute(
    "SELECT tag, path, line, text FROM rationales "
    "WHERE tag IN ('HACK','SAFETY','BUG','FIXME','WARNING','TODO','DEPRECATED')"
    " LIMIT 12").fetchall():
    all_rats.append(dict(r))
conn.close()

ctx = {
    "blast_radius_files":      len(br["affected_files"]),
    "blast_radius_sample":     br["affected_files"][:10],
    "callers_of_solve_dependencies": [
        {"caller": c["caller"], "path": c["path"], "bucket": c["bucket"]}
        for c in callers],
    "rationales_in_target_file": rats[:8],
    "all_fastapi_warning_rationales": all_rats,
    "god_nodes_top5": [
        {"name": g["qualified_name"], "callers": g["caller_count"]}
        for g in topo["god_nodes"][:5]],
    "pr_triage": {
        "risk_score":         triage["risk_score"],
        "risk_tier":          triage["risk_tier"],
        "god_nodes_touched":  triage["god_nodes_touched"],
        "merge_order_hint":   triage["merge_order_hint"],
    },
}
print(json.dumps({k: (v if not isinstance(v, list) else f"{len(v)} entries")
                   for k, v in ctx.items()}, indent=2))

# ── RUN 2 — with-graph agent ──────────────────────────────────────────
print("\n" + "=" * 78)
print("RUN 2 — WITH-GRAPH agent (same model, sees source + MCP context)")
print("=" * 78)

with_prompt = (
    f"You are a code reviewer. The file `{TARGET_FILE}` looks like this:\n\n"
    f"```python\n{src}\n```\n\n"
    f"You ALSO have these graph signals from the DecisionGraph code "
    f"intelligence layer:\n\n"
    f"```json\n{json.dumps(ctx, indent=2)[:6000]}\n```\n\n"
    f"QUESTION: {QUESTION}\n\n"
    f"Respond in MAX 12 bullet points. WHERE YOU USE A GRAPH SIGNAL, cite "
    f"it explicitly (e.g. 'blast_radius shows N files'). Cover: risks, "
    f"prior decisions/rationales captured, files to update, merge-order "
    f"guidance, god-node coordination needs.")

with_answer = call_llm(
    "You are a senior engineer reviewing a proposed change. "
    "When given graph signals, use them to ground your concrete claims.",
    with_prompt)
print(with_answer)

# ── Save both answers for comparison ──────────────────────────────────
Path("agent_compare.md").write_text(
    f"# QUESTION\n\n{QUESTION}\n\n"
    f"## RUN 1 — vanilla\n\n{vanilla_answer}\n\n"
    f"## RUN 2 — with-graph\n\n{with_answer}\n",
    encoding="utf-8")
print("\nSaved -> agent_compare.md")
