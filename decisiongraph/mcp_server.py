"""DecisionGraph MCP server — exposes the knowledge graph + decision memory
to any MCP client (Claude Desktop, Claude Code, custom agents).

Company-aware: every tool takes an optional `company_id` so external agents
can target a specific tenant. Without `company_id`, tools operate on the
personal/root graph. With `company_id`, queries hit both that company's
knowledge_dg and company_dg via multi_graph_beam_query.
"""
import json
import asyncio
from typing import Any, Optional
from mcp.server import Server
from mcp import types
from mcp.server.stdio import stdio_server

from .core import DecisionGraph
from .company import EnterpriseHub
from .query import beam_query, multi_graph_beam_query


# ──────────────────────────────────────────────────────────────────────────────
# Tool definitions (used by BOTH the stdio MCP server and the HTTP wrapper)
# ──────────────────────────────────────────────────────────────────────────────
TOOL_DEFS: list[types.Tool] = [
    types.Tool(
        name="list_companies",
        description="List all company workspaces available in DecisionGraph. Use this first to discover which `company_id` values you can pass to other tools.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="query_knowledge",
        description=(
            "Search the knowledge graph for context relevant to a question. "
            "Returns matched community ids and the relevant graph triples. "
            "If `company_id` is provided, queries both the company's knowledge "
            "graph AND its company-memory graph and merges the results (each "
            "triple tagged with its source). Otherwise queries the personal graph."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "question":   {"type": "string", "description": "Question or topic to search for"},
                "company_id": {"type": "string", "description": "Optional — target a specific company workspace"},
            },
            "required": ["question"],
        },
    ),
    types.Tool(
        name="get_past_decisions",
        description="Find past decisions similar to the current context. Always call this before making a new decision. With `company_id`, looks up the company's decision memory; otherwise the personal one.",
        inputSchema={
            "type": "object",
            "properties": {
                "context":    {"type": "string", "description": "Description of the current situation"},
                "company_id": {"type": "string", "description": "Optional — target a specific company"},
            },
            "required": ["context"],
        },
    ),
    types.Tool(
        name="store_decision",
        description="Persist a decision in DecisionGraph memory. With `company_id`, stores it under that company; otherwise the personal memory.",
        inputSchema={
            "type": "object",
            "properties": {
                "question":   {"type": "string", "description": "The question or situation that led to this decision"},
                "answer":     {"type": "string", "description": "The decision or answer reached"},
                "reasoning":  {"type": "string", "description": "One-line summary of the reasoning"},
                "company_id": {"type": "string", "description": "Optional — target a specific company"},
            },
            "required": ["question", "answer", "reasoning"],
        },
    ),
    types.Tool(
        name="ingest_document",
        description=(
            "Ingest a document (PDF/TXT/MD/DOCX/etc.) into the graph. "
            "With `company_id`, routes into that company; pick the sub-graph "
            "via `target` (\"knowledge\" for research/papers/techdocs, "
            "\"company\" for meeting notes/financials/policies). "
            "Without `company_id`, lands in the personal graph."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path":       {"type": "string", "description": "Filesystem path to the document"},
                "company_id": {"type": "string", "description": "Optional — target a specific company"},
                "target":     {"type": "string", "enum": ["knowledge", "company"],
                               "description": "When company_id is set: which sub-graph to ingest into (default: company)"},
            },
            "required": ["path"],
        },
    ),
    types.Tool(
        name="get_stats",
        description="Inspect graph and decision memory statistics. With `company_id`, returns that company's stats (both knowledge and company graphs); otherwise the personal graph.",
        inputSchema={
            "type": "object",
            "properties": {
                "company_id": {"type": "string", "description": "Optional — target a specific company"},
            },
        },
    ),
    types.Tool(
        name="recall",
        description="Fast NO-LLM recall: active, high-confidence decisions mentioning an entity (newest first) plus that entity's graph neighbourhood. Use before deep reasoning to check what's already known.",
        inputSchema={
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Person, company, project, or topic to recall"},
                "company_id": {"type": "string", "description": "Optional — target a specific company"},
                "limit": {"type": "integer", "description": "Max facts (default 8)"},
            },
            "required": ["entity"],
        },
    ),
    # ── Structural code-graph tools ──
    types.Tool(
        name="find_callers",
        description="Pure SQL query on the code graph: who calls a given function/class? Sub-second. Returns caller file, qualified name, and line numbers. Use BEFORE making a code change to understand impact.",
        inputSchema={"type":"object","properties":{
            "name":{"type":"string","description":"function or class name"},
            "repo":{"type":"string","description":"optional repo to scope to"},
            "limit":{"type":"integer"}},"required":["name"]},
    ),
    types.Tool(
        name="find_callees",
        description="Pure SQL query on the code graph: what does this function call? Returns callee names + resolved targets (with line numbers if known).",
        inputSchema={"type":"object","properties":{
            "name":{"type":"string"},"repo":{"type":"string"},
            "limit":{"type":"integer"}},"required":["name"]},
    ),
    types.Tool(
        name="blast_radius",
        description="What FILES break if I change this file? Walks the reverse call-graph + reverse imports up to N hops. Returns affected files grouped by hop distance. The killer feature for pre-edit safety checks.",
        inputSchema={"type":"object","properties":{
            "path":{"type":"string","description":"file path relative to repo root"},
            "repo":{"type":"string"},
            "max_depth":{"type":"integer","default":3}},"required":["path"]},
    ),
    types.Tool(
        name="find_call_path",
        description="BFS through the call graph: how does function A reach function B? Returns the shortest call chain or None.",
        inputSchema={"type":"object","properties":{
            "src":{"type":"string"},"dst":{"type":"string"},
            "max_hops":{"type":"integer","default":4},
            "repo":{"type":"string"}},"required":["src","dst"]},
    ),
    types.Tool(
        name="causal_radius",
        description="OUR EXTENSION beyond blast_radius: combines structural impact (affected files) with semantic context (related decisions, PRs, ADRs). Returns the full causal picture of a change — what breaks AND what was the original intent. Code-review-graph can't answer the intent half.",
        inputSchema={"type":"object","properties":{
            "path":{"type":"string"},"repo":{"type":"string"}},
            "required":["path"]},
    ),
    types.Tool(
        name="code_graph_stats",
        description="Counts in the structural code graph (files / symbols / calls / imports / inherits / rationales).",
        inputSchema={"type":"object","properties":{"repo":{"type":"string"}}},
    ),
    types.Tool(
        name="update_files",
        description=(
            "Incrementally update the code graph after editing files — INSTANT, "
            "no clone, no full re-ingest. Pass files=[{path, text}] with the FULL "
            "new content of each edited file. Call this right after writing code "
            "(NOT ingest_github). Structural tools become accurate immediately; "
            "files with new/removed symbols are flagged for lazy semantic refresh."),
        inputSchema={"type":"object","properties":{
            "files": {"type":"array","items":{"type":"object","properties":{
                "path": {"type":"string"}, "text": {"type":"string"}}}},
            "repo":  {"type":"string"}},
            "required":["files","repo"]},
    ),
    types.Tool(
        name="semantic_stale",
        description="List files whose semantic nodes are stale (edited since "
                    "last semantic pass) and pending lazy refresh.",
        inputSchema={"type":"object","properties":{"repo":{"type":"string"}}},
    ),
    types.Tool(
        name="get_context_pack",
        description=(
            "PHASE 2 / CAG: return a TIERED slice of the repo blueprint — the "
            "small repo overview (always) PLUS the per-file sections for the "
            "folders your `task_files` touch (on-demand). Fast (no graph load, "
            "no LLM). Preload this once at session start instead of the slow "
            "semantic query, so the 13s call never blocks work."),
        inputSchema={"type":"object","properties":{
            "task_files": {"type":"array","items":{"type":"string"}},
            "repo": {"type":"string"},
            "token_budget": {"type":"integer"},
            "include_folder_siblings": {"type":"boolean"}}},
    ),
    types.Tool(
        name="rationales",
        description=(
            "OUR EXTENSION: surface the `# WHY:` / `# HACK:` / `# SAFETY:` / "
            "`# TODO:` design rationales that are captured as first-class "
            "graph nodes linked to symbols. Pass either `symbol` (name) or "
            "`path` (file). Returns the rationale comments with line numbers — "
            "the tribal knowledge that usually dies in code review."),
        inputSchema={"type":"object","properties":{
            "symbol": {"type":"string"},
            "path":   {"type":"string"},
            "repo":   {"type":"string"}}},
    ),
    types.Tool(
        name="topology",
        description=(
            "Graph-shape analysis: god-nodes (highest-connectivity symbols — "
            "chokepoints), surprising-connections (unusual cross-folder edges), "
            "orphan symbols (likely dead code), and deep inheritance chains. "
            "Pure SQL — no LLM cost."),
        inputSchema={"type":"object","properties":{
            "repo":         {"type":"string"},
            "top_god":      {"type":"integer"},
            "top_surprise": {"type":"integer"}}},
    ),
    types.Tool(
        name="suggested_questions",
        description=(
            "Auto-generated onboarding questions the graph is uniquely "
            "positioned to answer. Use as a starting prompt for new joiners "
            "or new AI sessions."),
        inputSchema={"type":"object","properties":{"repo":{"type":"string"}}},
    ),
    types.Tool(
        name="export_graph_html",
        description=(
            "Export an interactive HTML visualization of the structural code "
            "graph. Self-contained file. God-nodes red, edges colored by "
            "confidence bucket. `focus_path` limits to one file + its neighbours."),
        inputSchema={"type":"object","properties":{
            "out_path":   {"type":"string"},
            "repo":       {"type":"string"},
            "focus_path": {"type":"string"},
            "max_nodes":  {"type":"integer"}}},
    ),
    types.Tool(
        name="export_graph_cypher",
        description="Export the code graph as Neo4j Cypher load statements.",
        inputSchema={"type":"object","properties":{
            "out_path": {"type":"string"},
            "repo":     {"type":"string"}}},
    ),
    types.Tool(
        name="federated_callers",
        description=(
            "Find callers of a symbol across MULTIPLE workspaces / repos. "
            "Pass a list of `workspace_roots` (directories containing "
            "code_graph.db files); we union the results."),
        inputSchema={"type":"object","properties":{
            "symbol":          {"type":"string"},
            "workspace_roots": {"type":"array","items":{"type":"string"}},
            "limit_per_db":    {"type":"integer"}},
            "required":["symbol"]},
    ),
    types.Tool(
        name="federated_topology",
        description=(
            "Cross-repo god-nodes: symbols with the same leaf-name that are "
            "chokepoints in MULTIPLE repos. Use for org-wide refactor risk "
            "assessment."),
        inputSchema={"type":"object","properties":{
            "workspace_roots": {"type":"array","items":{"type":"string"}},
            "top_god":         {"type":"integer"}}},
    ),
    types.Tool(
        name="federated_rationale_search",
        description=(
            "Search inline `# HACK:` / `# WHY:` / `# SAFETY:` rationale "
            "comments across all repos."),
        inputSchema={"type":"object","properties":{
            "query":           {"type":"string"},
            "tag":             {"type":"string"},
            "workspace_roots": {"type":"array","items":{"type":"string"}}},
            "required":["query"]},
    ),

    types.Tool(
        name="triage_pr",
        description=(
            "OUR EXTENSION: PR risk scoring. Pass a list of changed files; "
            "get combined blast-radius, god-node touches, rationale warnings "
            "(HACK/SAFETY/BUG/FIXME near touched code), a 0-100 risk score, "
            "and a merge-order hint. Fuses structural impact + captured "
            "design rationales — code-review-graph has triage too but no "
            "rationale layer."),
        inputSchema={"type":"object","properties":{
            "changed_files": {"type":"array","items":{"type":"string"}},
            "repo":          {"type":"string"},
            "max_hops":      {"type":"integer"}},
            "required":["changed_files"]},
    ),

    types.Tool(
        name="get_onboarding_brief",
        description=(
            "Return a one-shot 'what to know' bundle for someone (or some AI) "
            "starting work in this codebase: repo overview, active topics, "
            "most recent decisions, recently-edited files, and which AI agents "
            "have been touching the code. Includes a ready-to-paste brief_text "
            "field you can drop directly into a system prompt."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic":           {"type": "string", "description": "Optional — narrow to one topic"},
                "limit_decisions": {"type": "integer"},
                "limit_edits":     {"type": "integer"},
                "limit_topics":    {"type": "integer"},
                "company_id":      {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="ingest_github",
        description=(
            "Ingest a public github.com repository into the user's DG. The "
            "platform shallow-clones, runs tree-sitter AST chunking, summarises "
            "every function/class/file/folder with the LLM, builds a "
            "function-call graph, captures recent PRs, and uses an incremental "
            "hash cache so re-runs of the same repo only re-process changed "
            "files. Call this so the agent has full context of an unfamiliar "
            "repo before doing any work on it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_url":             {"type": "string"},
                "branch":               {"type": "string"},
                "include_code":         {"type": "boolean"},
                "use_ast":              {"type": "boolean"},
                "include_call_edges":   {"type": "boolean"},
                "include_hierarchical": {"type": "boolean"},
                "include_prs":          {"type": "boolean"},
                "incremental":          {"type": "boolean"},
                "company_id":           {"type": "string"},
            },
            "required": ["repo_url"],
        },
    ),
    types.Tool(
        name="track_code_edit",
        description=(
            "Capture a code edit you (the agent) just made. Pass before+after "
            "text — the platform computes a unified diff and stores it as a "
            "permanent decision tagged with WHO did it, WHEN, WHERE (file:line), "
            "WHY (your reasoning + the user's prompt). Call this AFTER every "
            "meaningful code change so future sessions can audit why."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_path":  {"type": "string"},
                "before":     {"type": "string"},
                "after":      {"type": "string"},
                "reasoning":  {"type": "string"},
                "prompt":     {"type": "string"},
                "line_range": {"type": "string"},
                "repo":       {"type": "string"},
                "company_id": {"type": "string"},
            },
            "required": ["file_path"],
        },
    ),
    types.Tool(
        name="track_decision",
        description="Record an arbitrary decision that did NOT involve a code edit (e.g. tech-stack choices, design calls). Becomes a permanent decision node.",
        inputSchema={
            "type": "object",
            "properties": {
                "question":   {"type": "string"},
                "answer":     {"type": "string"},
                "reasoning":  {"type": "string"},
                "topic":      {"type": "string"},
                "company_id": {"type": "string"},
            },
            "required": ["question", "answer"],
        },
    ),
    types.Tool(
        name="recent_code_edits",
        description="Return the N most recent AI-driven code edits captured in this workspace. Use for audit, change history, and giving the next agent context about what's been touched lately.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit":     {"type": "integer"},
                "file_path": {"type": "string"},
                "company_id":{"type": "string"},
            },
        },
    ),
]


# ──────────────────────────────────────────────────────────────────────────────
# Pure async handlers — take (dg, hub, dm, **args), return JSON-serialisable dict
# Both the stdio MCP server and the HTTP wrapper call these.
# ──────────────────────────────────────────────────────────────────────────────
async def _h_list_companies(dg, hub, dm, **_) -> dict:
    return {"companies": [
        {"id": cid, "name": cm.company_name,
         "documents": len(cm.registry),
         "knowledge_nodes": cm.knowledge_dg.G.number_of_nodes() if cm.knowledge_dg.G else 0,
         "company_nodes":   cm.company_dg.G.number_of_nodes()   if cm.company_dg.G   else 0}
        for cid, cm in hub.companies.items()
    ]}


async def _h_query_knowledge(dg, hub, dm, question: str, company_id: Optional[str] = None, **_) -> dict:
    if company_id:
        cm = hub.get_company(company_id)
        if not cm:
            return {"error": f"Company '{company_id}' not found"}
        graphs = cm._graphs_payload()
        if not graphs:
            return {"scope": f"company:{company_id}", "context": [], "hit_report": {},
                    "note": "Company has no graph data yet."}
        triples, report = multi_graph_beam_query(question, graphs, cm.company_dg.embed_model)
        return {"scope": f"company:{company_id}", "context": triples[:30], "hit_report": report}
    # personal graph
    if dg.G is None:
        return {"error": "Personal graph empty. Ingest documents first."}
    triples, matched = beam_query(question, dg.G, dg.summaries,
                                  dg.community_ids, dg.community_embeddings, dg.embed_model)
    return {"scope": "personal", "context": triples[:30], "communities_matched": matched}


async def _h_get_past_decisions(dg, hub, dm, context: str, company_id: Optional[str] = None, **_) -> dict:
    if company_id:
        cm = hub.get_company(company_id)
        if not cm: return {"error": f"Company '{company_id}' not found"}
        past = cm.company_dg.memory.query(context, cm.company_dg.embed_model)
    else:
        past = dg.memory.query(context, dg.embed_model)
    return {"past_decisions": past or [], "scope": f"company:{company_id}" if company_id else "personal"}


async def _h_store_decision(dg, hub, dm, question: str, answer: str, reasoning: str,
                            company_id: Optional[str] = None, **_) -> dict:
    if company_id:
        cm = hub.get_company(company_id)
        if not cm: return {"error": f"Company '{company_id}' not found"}
        with cm._scoped_storage(cm.company_subdir):
            did = cm.company_dg.memory.store(question=question, answer=answer,
                                             reasoning_summary=reasoning,
                                             communities_used=[], context_triples=[])
            cm.company_dg.memory.save()
    else:
        did = dg.memory.store(question=question, answer=answer,
                              reasoning_summary=reasoning,
                              communities_used=[], context_triples=[])
        dg.memory.save()
    return {"id": did, "scope": f"company:{company_id}" if company_id else "personal"}


async def _h_ingest_document(dg, hub, dm, path: str, company_id: Optional[str] = None,
                             target: str = "company", **_) -> dict:
    import os
    if not os.path.exists(path):
        return {"error": f"Path does not exist: {path}"}
    if company_id:
        cm = hub.get_company(company_id)
        if not cm: return {"error": f"Company '{company_id}' not found"}
        if target == "knowledge":
            did = cm.ingest_knowledge(path)
        else:
            did = cm.ingest_company(path)
        return {"ok": True, "doc_id": did, "scope": f"company:{company_id}/{target}", "stats": cm.stats()}
    dg.ingest(path)
    return {"ok": True, "scope": "personal", "stats": dg.stats()}


async def _h_get_stats(dg, hub, dm, company_id: Optional[str] = None, **_) -> dict:
    if company_id:
        cm = hub.get_company(company_id)
        if not cm: return {"error": f"Company '{company_id}' not found"}
        return {"scope": f"company:{company_id}", **cm.stats()}
    return {"scope": "personal", **dg.stats()}


async def _h_recall(dg, hub, dm, entity: str = "", company_id: str = None, limit: int = 8):
    """gbrain #3 — fast no-LLM recall."""
    target = dg
    if company_id and hub:
        cm = hub.get_company(company_id)
        if cm: target = cm.company_dg
    e = (entity or "").strip().lower()
    if not e: return {"error": "entity required"}
    facts = []
    for d in target.memory.get_active_decisions():
        if e in f"{d.get('question','')} {d.get('answer','')}".lower():
            facts.append(d)
    facts.sort(key=lambda d: (d.get("confidence", 0), d.get("timestamp", "")), reverse=True)
    nb = []
    G = getattr(target, "G", None)
    if G is not None:
        for n in G.nodes():
            if e in str(n).lower():
                for u, v, data in list(G.edges(n, data=True))[:10]:
                    nb.append(f"{u} --[{data.get('relation','')}]--> {v}")
                break
    return {"entity": entity, "facts": facts[:limit], "graph_neighbourhood": nb[:10]}


# ── AI-edit capture handlers ────────────────────────────────────────────────
# ── Structural code-graph tools (SQLite, no LLM, sub-second) ─────────────────
def _cg_db_path(dg) -> str:
    """Resolve the code_graph SQLite path.

    Search order:
      1. DG_CODE_GRAPH_DB env var (explicit override)
      2. .dg_code_graph.db in current working directory (what
         `code_graph_cli install-hook` writes by default — the typical
         "Claude-Code-in-my-repo" case)
      3. {dg.storage_dir}/code_graph.db (workspace-style location)
      4. Repo-style ./.dg_code_graph.db relative to dg.storage_dir's parent
    First existing file wins; otherwise return the storage_dir path so
    callers get a clear "file not found" error rather than silent zeros.
    """
    import os
    # 1. Explicit env override — return verbatim even if file doesn't exist
    #    yet (lets users pre-configure the path before ingest runs).
    env = os.environ.get("DG_CODE_GRAPH_DB")
    if env:
        return env
    # 2-4. Existence-checked search order:
    candidates = [
        os.path.join(os.getcwd(), ".dg_code_graph.db"),
        os.path.join(dg.storage_dir, "code_graph.db"),
        os.path.join(os.path.dirname(dg.storage_dir.rstrip("/\\")),
                      ".dg_code_graph.db"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[1]    # workspace fallback


async def _h_find_callers(dg, hub, dm, name: str = "", repo: str = None,
                            limit: int = 50, **_) -> dict:
    """Who calls function/class `name`? Pure SQL on the code graph."""
    if not name: return {"error": "name required"}
    from .code_graph import find_callers
    try:
        callers = find_callers(_cg_db_path(dg), name, repo=repo, limit=int(limit))
        return {"name": name, "callers": callers, "count": len(callers)}
    except Exception as e:
        return {"error": f"find_callers failed: {e}"}


async def _h_find_callees(dg, hub, dm, name: str = "", repo: str = None,
                            limit: int = 50, **_) -> dict:
    """What does `name` call? Pure SQL on the code graph."""
    if not name: return {"error": "name required"}
    from .code_graph import find_callees
    try:
        callees = find_callees(_cg_db_path(dg), name, repo=repo, limit=int(limit))
        return {"name": name, "callees": callees, "count": len(callees)}
    except Exception as e:
        return {"error": f"find_callees failed: {e}"}


async def _h_blast_radius(dg, hub, dm, path: str = "", repo: str = None,
                            max_depth: int = 3, **_) -> dict:
    """What files break if I change `path`? Walks the reverse call-graph
    + reverse imports. Same idea as code-review-graph's flagship feature."""
    if not path: return {"error": "path required"}
    from .code_graph import blast_radius
    try:
        return blast_radius(_cg_db_path(dg), path, repo=repo,
                              max_depth=int(max_depth))
    except Exception as e:
        return {"error": f"blast_radius failed: {e}"}


async def _h_find_call_path(dg, hub, dm, src: str = "", dst: str = "",
                              max_hops: int = 4, repo: str = None, **_) -> dict:
    """Find a call chain from `src` to `dst`. Returns the shortest path
    through the call graph, or None if no path exists within max_hops."""
    if not src or not dst: return {"error": "src and dst required"}
    from .code_graph import find_path
    try:
        path = find_path(_cg_db_path(dg), src, dst, max_hops=int(max_hops),
                          repo=repo)
        return {"src": src, "dst": dst, "path": path,
                "found": path is not None,
                "hops": (len(path) - 1) if path else 0}
    except Exception as e:
        return {"error": f"find_path failed: {e}"}


async def _h_causal_radius(dg, hub, dm, path: str = "", repo: str = None,
                             **_) -> dict:
    """OUR EXTENSION beyond code-review-graph: combine structural blast radius
    with decision memory. Returns:
      • affected files (their thing — from call graph + imports)
      • related decisions (our thing — from DG memory tagged with this repo)
      • PRs that touched this file (from ingested PR data)
    The full causal picture for a change, not just downstream code."""
    if not path: return {"error": "path required"}
    from .code_graph import blast_radius
    out = {"changed_file": path, "repo": repo}
    try:
        br = blast_radius(_cg_db_path(dg), path, repo=repo, max_depth=3)
        out.update({
            "affected_files":           br["affected_files"],
            "symbols_in_changed_file":  br["symbols_in_changed_file"],
            "callers_by_hop":           br["callers_by_hop"],
            "reverse_imports":          br["reverse_imports"],
            "total_affected":           br["total_affected"],
        })
    except Exception as e:
        out["blast_radius_error"] = str(e)

    # ── decisions / ADRs that mention this file ──────────────────────────
    try:
        rows = dg.memory.all_decisions()
        related = []
        import os
        base = os.path.basename(path)
        for r in rows:
            q  = (r.get("question") or "")
            rs = (r.get("reasoning_summary") or "")
            ans = (r.get("answer") or "")
            blob = (q + " " + rs + " " + ans).lower()
            if path.lower() in blob or base.lower() in blob:
                related.append({
                    "id":         r.get("id"),
                    "question":   q[:160],
                    "snippet":    ans[:200],
                    "timestamp":  r.get("timestamp"),
                })
        related.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
        out["related_decisions"] = related[:30]
        out["total_related_decisions"] = len(related)
    except Exception as e:
        out["decisions_error"] = str(e)

    # ── README / blueprint context for the affected symbols ──────────────
    # This is the bit code-review-graph CAN'T do: surface the original prose
    # describing what the changed file is FOR, pulled from the semantic
    # blueprint we ingested (README, docstrings, ADRs).
    try:
        symbols = out.get("symbols_in_changed_file") or []
        snippets: list[dict] = []
        # search DG decision nodes for prose mentioning each symbol
        rows = dg.memory.all_decisions()
        wanted = set(s.lower() for s in symbols if s and len(s) > 2)
        for r in rows:
            text = ((r.get("answer") or "") + " "
                    + (r.get("reasoning_summary") or "")).lower()
            for sym in wanted:
                if sym in text:
                    snippets.append({
                        "symbol":    sym,
                        "context":   (r.get("answer") or "")[:240],
                        "decision":  r.get("id"),
                    })
                    break
        out["readme_blueprint_context"] = snippets[:10]
    except Exception as e:
        out["context_error"] = str(e)

    # ── recent PRs that touched this file ────────────────────────────────
    # Pulled from the PR data ingested during repo blueprint creation.
    try:
        # Match `[pr:#NNN]` or `[PR #NNN]` style question prefixes.
        # (DG memory doesn't carry a separate `topic` field — info is in question.)
        pr_rows = [r for r in dg.memory.all_decisions()
                   if (r.get("question") or "").lower().startswith("[pr")
                   or "[pr #" in (r.get("question") or "").lower()]
        prs_for_file = []
        import os
        base = os.path.basename(path)
        for r in pr_rows:
            blob = ((r.get("question") or "") + " "
                    + (r.get("answer") or "")).lower()
            if path.lower() in blob or base.lower() in blob:
                prs_for_file.append({
                    "id":       r.get("id"),
                    "title":    (r.get("question") or "")[:160],
                    "summary":  (r.get("answer") or "")[:240],
                    "ts":       r.get("timestamp"),
                })
        out["related_prs"] = prs_for_file[:10]
    except Exception as e:
        out["prs_error"] = str(e)

    # ── recent commits touching this file (from git-log ingest) ──────────
    try:
        commit_rows = [r for r in dg.memory.all_decisions()
                       if (r.get("question") or "").lower().startswith("[commit")
                       or "[commit:" in (r.get("question") or "").lower()
                       or "[commit ]" in (r.get("question") or "").lower()]
        commits_for_file = []
        import os
        base = os.path.basename(path)
        for r in commit_rows:
            blob = ((r.get("question") or "") + " "
                    + (r.get("answer") or "")).lower()
            if path.lower() in blob or base.lower() in blob:
                commits_for_file.append({
                    "id":       r.get("id"),
                    "message":  (r.get("question") or "")[:200],
                    "context":  (r.get("answer") or "")[:240],
                    "ts":       r.get("timestamp"),
                })
        out["related_commits"] = commits_for_file[:10]
    except Exception as e:
        out["commits_error"] = str(e)

    return out


async def _h_code_graph_stats(dg, hub, dm, repo: str = None, **_) -> dict:
    """Counts in the structural code graph."""
    from .code_graph import stats
    try:
        return stats(_cg_db_path(dg), repo=repo)
    except Exception as e:
        return {"error": f"stats failed: {e}"}


async def _h_update_files(dg, hub, dm, files: list = None, repo: str = "",
                            **_) -> dict:
    """Incrementally update the code graph after editing files — INSTANT,
    no clone, no full re-ingest. Pass the files you just edited:
        files = [{"path": "src/x.py", "text": "<full new content>"}, ...]

    Structural graph (find_callers / blast_radius / triage_pr) becomes
    accurate immediately. Files with NEW/REMOVED symbols or that touch a
    god-node are flagged 'major' and marked for a lazy semantic refresh.

    This is the right tool to call right after writing code — NOT
    ingest_github (which re-clones the whole repo from GitHub)."""
    from .code_graph import update_files
    if not files:
        return {"error": "files required: [{path, text}, ...]"}
    if not repo:
        return {"error": "repo required (e.g. 'owner/name')"}
    try:
        return update_files(_cg_db_path(dg), list(files), repo)
    except Exception as e:
        return {"error": f"update_files failed: {e}"}


async def _h_semantic_stale(dg, hub, dm, repo: str = None, **_) -> dict:
    """List files whose structural graph is current but whose semantic nodes
    are stale (edited since the last semantic pass)."""
    from .code_graph import list_semantic_stale
    try:
        rows = list_semantic_stale(_cg_db_path(dg), repo=repo)
        return {"stale_files": rows, "count": len(rows)}
    except Exception as e:
        return {"error": f"semantic_stale failed: {e}"}


def _find_blueprint(dg, repo: str = "") -> str:
    """Locate the blueprint .md for a repo inside the workspace's storage dir.
    If `repo` is given, sanitize it the same way ingest does. If omitted and
    exactly one blueprint exists, use it. Returns '' if none/ambiguous."""
    import os, re as _re
    bp_dir = os.path.join(dg.storage_dir, "blueprints")
    if not os.path.isdir(bp_dir):
        return ""
    if repo:
        safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", repo.replace("/", "_"))[:120]
        cand = os.path.join(bp_dir, f"{safe}.md")
        if os.path.exists(cand):
            return cand
    mds = [f for f in os.listdir(bp_dir) if f.endswith(".md")]
    if len(mds) == 1:
        return os.path.join(bp_dir, mds[0])
    return ""


async def _h_get_context_pack(dg, hub, dm, task_files: list = None,
                                repo: str = "", token_budget: int = 6000,
                                include_folder_siblings: bool = True, **_) -> dict:
    """PHASE 2 / CAG: return a tiered slice of the repo blueprint (DG-CAG).

    ALWAYS includes the small repo overview (mission/concepts/stack); ON-DEMAND
    includes the per-file sections for the folders `task_files` touch. Fast —
    pure text, NO graph load, NO LLM — so the slow semantic layer never blocks
    the agent. Preload this once at session start instead of the 13s query."""
    import os
    from .context_pack import get_context_pack
    bp = _find_blueprint(dg, repo)
    if not bp:
        bp_dir = os.path.join(dg.storage_dir, "blueprints")
        avail = []
        if os.path.isdir(bp_dir):
            avail = [f[:-3] for f in os.listdir(bp_dir) if f.endswith(".md")]
        return {"error": "blueprint not found; pass repo=", "available": avail}
    try:
        pack = get_context_pack(bp, task_files or [], token_budget=token_budget,
                                include_folder_siblings=include_folder_siblings)
        pack["blueprint"] = os.path.basename(bp)
        return pack
    except Exception as e:
        return {"error": f"get_context_pack failed: {e}"}


async def _h_rationales(dg, hub, dm, symbol: str = "", path: str = "",
                          repo: str = None, **_) -> dict:
    """Surface the `# WHY:` / `# HACK:` / `# SAFETY:` design rationales
    captured during ingest. Either pass a symbol name OR a file path.

    OUR EXTENSION: code-review-graph parses comments only as freeform text;
    we treat tagged rationales as first-class nodes linked to symbols, so
    you can ask 'why is this code like this?' and get the actual author note."""
    from .code_graph import rationales_for_symbol, rationales_in_file
    db = _cg_db_path(dg)
    try:
        if symbol:
            return {"symbol": symbol,
                    "rationales": rationales_for_symbol(db, symbol, repo=repo)}
        if path:
            return {"path": path,
                    "rationales": rationales_in_file(db, path, repo=repo)}
        return {"error": "pass either `symbol` or `path`"}
    except Exception as e:
        return {"error": f"rationales failed: {e}"}


async def _h_topology(dg, hub, dm, repo: str = None,
                        top_god: int = 15, top_surprise: int = 15, **_) -> dict:
    """Graph-topology insights: god-nodes (chokepoints), surprising
    cross-folder connections, orphan symbols (likely dead code), and deep
    inheritance chains. Pure SQL — no LLM."""
    from .code_graph import analyze_topology
    try:
        return analyze_topology(_cg_db_path(dg), repo=repo,
                                top_god=int(top_god),
                                top_surprise=int(top_surprise))
    except Exception as e:
        return {"error": f"topology failed: {e}"}


async def _h_suggested_questions(dg, hub, dm, repo: str = None, **_) -> dict:
    """Auto-generated onboarding questions: 'things the graph is uniquely
    positioned to answer.' Used to bootstrap new joiners or new AI sessions."""
    from .code_graph import suggested_questions
    try:
        return {"questions": suggested_questions(_cg_db_path(dg), repo=repo)}
    except Exception as e:
        return {"error": f"suggested_questions failed: {e}"}


async def _h_export_graph_html(dg, hub, dm, out_path: str = "graph.html",
                                  repo: str = None, focus_path: str = None,
                                  max_nodes: int = 500, **_) -> dict:
    """Export an interactive standalone HTML visualization of the code
    graph. Open the file in any browser. Highlights god-nodes in red,
    colors edges by confidence bucket."""
    from .code_graph_viz import export_html
    try:
        return export_html(_cg_db_path(dg), out_path=out_path, repo=repo,
                            focus_path=focus_path or None,
                            max_nodes=int(max_nodes))
    except Exception as e:
        return {"error": f"export_html failed: {e}"}


async def _h_export_graph_cypher(dg, hub, dm, out_path: str = "graph.cypher",
                                    repo: str = None, **_) -> dict:
    """Export the code graph as Cypher load statements (Neo4j-importable)."""
    from .code_graph_viz import export_cypher
    try:
        return export_cypher(_cg_db_path(dg), out_path=out_path, repo=repo)
    except Exception as e:
        return {"error": f"export_cypher failed: {e}"}


async def _h_federated_callers(dg, hub, dm, symbol: str = "",
                                  workspace_roots: list = None,
                                  limit_per_db: int = 20, **_) -> dict:
    """Find callers across MULTIPLE workspaces / code_graph dbs at once."""
    from .code_graph_federated import federated_find_callers
    if not symbol: return {"error": "symbol required"}
    roots = workspace_roots or ["storage/workspaces", "storage/bench_graphs"]
    try:
        return federated_find_callers(roots, symbol, int(limit_per_db))
    except Exception as e:
        return {"error": f"federated_callers failed: {e}"}


async def _h_federated_topology(dg, hub, dm,
                                   workspace_roots: list = None,
                                   top_god: int = 10, **_) -> dict:
    """God-nodes and stats across ALL workspaces — cross-repo chokepoint
    detection. A symbol that's a god-node in 3 different repos is probably
    a fundamental concept."""
    from .code_graph_federated import federated_topology
    roots = workspace_roots or ["storage/workspaces", "storage/bench_graphs"]
    try:
        return federated_topology(roots, int(top_god))
    except Exception as e:
        return {"error": f"federated_topology failed: {e}"}


async def _h_federated_rationale_search(dg, hub, dm, query: str = "",
                                            tag: str = None,
                                            workspace_roots: list = None,
                                            **_) -> dict:
    """Search inline rationale comments across all repos. E.g. `# HACK:`
    mentioning 'race condition' across every repo your company owns."""
    from .code_graph_federated import federated_rationales_search
    if not query: return {"error": "query required"}
    roots = workspace_roots or ["storage/workspaces", "storage/bench_graphs"]
    try:
        return federated_rationales_search(roots, query, tag=tag)
    except Exception as e:
        return {"error": f"federated_rationales_search failed: {e}"}


async def _h_triage_pr(dg, hub, dm, changed_files: list = None,
                         repo: str = None, max_hops: int = 3, **_) -> dict:
    """PR triage scoring. Pass a list of files in a PR; get a combined
    blast-radius, god-node touch flags, rationale warnings, a 0-100 risk
    score, and a merge-order hint."""
    from .code_graph import triage_pr
    if not changed_files: return {"error": "changed_files required"}
    try:
        return triage_pr(_cg_db_path(dg), list(changed_files),
                          repo=repo, max_hops=int(max_hops))
    except Exception as e:
        return {"error": f"triage_pr failed: {e}"}


async def _h_get_onboarding_brief(dg, hub, dm,
                                     topic: str = "",
                                     limit_decisions: int = 8,
                                     limit_edits: int = 6,
                                     limit_topics: int = 5,
                                     company_id: str = None, **_) -> dict:
    """One-shot 'what do I need to know to start working in this codebase'
    bundle for a new joiner (or a new AI session). Aggregates:
      • repo overview (latest repo-level summary, if any)
      • top recent decisions (filtered by topic if given)
      • most-recently-edited files (from track_code_edit captures)
      • top topics with quick descriptions
      • current AI agents in the system + their reputation standings
    Returns a single JSON payload ready to inject into an AI's system prompt."""
    target = dg
    if company_id and hub:
        cm = hub.get_company(company_id)
        if cm: target = cm.company_dg

    # ── repo overview: most recent [repo:*] · repo · overview decision
    repo_overview = ""
    repo_name = ""
    try:
        rows = target.memory.all_decisions()
        ov_rows = [r for r in rows
                    if (r.get("question") or "").startswith("[repo:")
                    and "repo · overview" in (r.get("question") or "")]
        ov_rows.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
        if ov_rows:
            repo_overview = (ov_rows[0].get("answer") or "")[:2000]
            # extract "[repo:NAME] ..." → NAME
            import re as _re
            m = _re.match(r"\[repo:([^\]]+)\]", ov_rows[0].get("question") or "")
            if m: repo_name = m.group(1)
    except Exception:
        rows = []

    # ── top recent decisions (filter by topic if given)
    recent_decisions = []
    for r in rows:
        q = (r.get("question") or "")
        if not q:
            continue
        if topic and topic.lower() not in (q + " " + (r.get("answer") or "")).lower():
            continue
        # skip the meta-overview entries from above
        if q.startswith("[edit") or "repo · overview" in q:
            continue
        recent_decisions.append({
            "id":         r.get("id"),
            "question":   q[:200],
            "answer":     (r.get("answer") or "")[:280],
            "timestamp":  r.get("timestamp"),
            "confidence": r.get("confidence"),
        })
    recent_decisions.sort(key=lambda d: d.get("timestamp") or "", reverse=True)
    recent_decisions = recent_decisions[: max(1, min(int(limit_decisions), 50))]

    # ── most-recently-edited files (from track_code_edit)
    recent_edits = []
    for r in rows:
        q = r.get("question") or ""
        if not (q.startswith("[edit]") or q.startswith("[edit:")):
            continue
        recent_edits.append({
            "summary":   q[:150],
            "timestamp": r.get("timestamp"),
        })
    recent_edits.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    recent_edits = recent_edits[: max(1, min(int(limit_edits), 50))]

    # ── top topics: detected communities + their compiled-truth summaries
    topics_out = []
    try:
        compiled = target.memory.all_compiled() or {}
        # all_compiled returns dict keyed by community_id → {summary, ...}
        items = []
        if isinstance(compiled, dict):
            for cid, payload in compiled.items():
                if isinstance(payload, dict):
                    items.append({
                        "id":      cid,
                        "summary": (payload.get("compiled") or payload.get("summary") or "")[:300],
                    })
        items = [t for t in items if t["summary"]]
        topics_out = items[: max(1, min(int(limit_topics), 20))]
    except Exception:
        topics_out = []

    # ── agent reputation summary (just counts + top standings)
    rep_summary = {}
    try:
        # we don't have ws here in mcp_server context, so import the store
        from .agent_access import AgentAccessStore
        from . import config as _cfg
        store = AgentAccessStore(target.storage_dir if hasattr(target, "storage_dir") else _cfg.STORAGE_DIR)
        from .agent_reputation import compute_reputation
        rep = compute_reputation(store)
        top = sorted(rep, key=lambda x: -x.get("score", 0))[:5]
        rep_summary = {
            "total_agents": len(rep),
            "top_agents":   [
                {"name": a.get("agent_name"), "score": a.get("score"),
                 "standing": a.get("standing"),
                 "jobs_completed": a.get("jobs_completed", 0)}
                for a in top
            ],
        }
    except Exception:
        rep_summary = {"total_agents": 0, "top_agents": []}

    # ── stitched briefing text — ready to drop into a system prompt
    lines = ["═══ ONBOARDING BRIEF — what to know about this codebase ═══", ""]
    if repo_name:
        lines.append(f"REPO: {repo_name}")
    if repo_overview:
        lines += ["", "OVERVIEW:", repo_overview, ""]
    if topics_out:
        lines.append("ACTIVE TOPICS:")
        for t in topics_out:
            lines.append(f"  • [{t['id']}] {t['summary'][:200]}")
        lines.append("")
    if recent_decisions:
        lines.append(f"RECENT DECISIONS (most recent first, top {len(recent_decisions)}):")
        for d in recent_decisions:
            lines.append(f"  • {d['question']}")
            if d['answer']:
                lines.append(f"      → {d['answer'][:200]}")
        lines.append("")
    if recent_edits:
        lines.append(f"RECENTLY EDITED ({len(recent_edits)} captures):")
        for e in recent_edits:
            lines.append(f"  • {e['summary']}")
        lines.append("")
    if rep_summary.get("top_agents"):
        lines.append("ACTIVE AI AGENTS:")
        for a in rep_summary["top_agents"]:
            lines.append(f"  • {a['name']} — {a['standing']} (score {a['score']}, {a['jobs_completed']} jobs)")
    brief_text = "\n".join(lines)

    return {
        "repo_name":       repo_name,
        "repo_overview":   repo_overview,
        "topics":          topics_out,
        "recent_decisions": recent_decisions,
        "recent_edits":    recent_edits,
        "agent_reputation": rep_summary,
        "brief_text":      brief_text,
    }


async def _h_ingest_github(dg, hub, dm, repo_url: str = "",
                              branch: str = "", include_code: bool = True,
                              use_ast: bool = True,
                              include_call_edges: bool = True,
                              include_hierarchical: bool = True,
                              include_prs: bool = True,
                              incremental: bool = True,
                              company_id: str = None, **_) -> dict:
    """Ingest a public github.com URL into the worker's DG.

    Behaves like the UI: queues the full 6-layer pipeline as a background
    job and returns the job_id IMMEDIATELY so the MCP client doesn't time
    out. The agent should poll `job_status(job_id)` until status='done'.

    Full pipeline:
      1. shallow git clone
      2. AST chunks (tree-sitter) + call edges
      3. README / ADRs / CHANGELOG / manifests / docs
      4. per-file LLM role summaries
      5. folder/repo hierarchical roll-ups
      6. recent PRs (GitHub API)

    Typical timing: 1-3 minutes on flask-sized repos.

    Set include_code/include_hierarchical/include_prs=False for a faster
    structural-only ingest (5-15s, no LLM)."""
    if not repo_url:
        return {"error": "repo_url required"}
    from .codebase import _parse_github_url
    if not _parse_github_url(repo_url):
        return {"error": "must be a public github.com URL"}

    # Try to queue via the JOBS queue (works when the MCP request was
    # dispatched through server.py — the typical HTTP-MCP / Claude-Code
    # path). This is the SAME async path the UI uses.
    try:
        from server import JOBS, _CURRENT_WS    # type: ignore
        ws = _CURRENT_WS.get()
        ws_token = ws.token if ws else "default"
        jid = JOBS.enqueue("ingest_github", {
            "workspace":            ws_token,
            "repo_url":             repo_url,
            "branch":               branch or "",
            "include_code":         bool(include_code),
            "use_ast":              bool(use_ast),
            "include_call_edges":   bool(include_call_edges),
            "include_hierarchical": bool(include_hierarchical),
            "include_prs":          bool(include_prs),
            "incremental":          bool(incremental),
        }, workspace=ws_token)
        return {
            "job_id":   jid,
            "status":   "queued",
            "source":   "github",
            "repo_url": repo_url,
            "next":     f"call job_status with job_id={jid} to poll progress; "
                        f"typical ingest is 1-3 minutes",
        }
    except Exception:
        pass    # fall through to synchronous path (stdio MCP standalone)

    # Synchronous fallback (stdio mode, no JOBS queue available).
    # Will block until done — clients may time out on large repos.
    from .codebase import ingest_github_url
    target = dg
    if company_id and hub:
        cm = hub.get_company(company_id)
        if cm: target = cm.company_dg
    try:
        result = ingest_github_url(
            target, repo_url,
            branch=(branch or None),
            include_code_summaries=bool(include_code),
            use_ast=bool(use_ast),
            include_call_edges=bool(include_call_edges),
            include_hierarchical=bool(include_hierarchical),
            include_prs=bool(include_prs),
            incremental=bool(incremental))
        return {"status": "done", "synchronous": True, **(result or {})}
    except Exception as e:
        return {"error": f"github ingest failed: {e}"}


async def _h_track_code_edit(dg, hub, dm, file_path: str = "",
                                before: str = "", after: str = "",
                                reasoning: str = "", prompt: str = "",
                                line_range: str = "", repo: str = "",
                                company_id: str = None, **_) -> dict:
    """Capture an AI-driven code edit as a permanent decision in the DG.
    Stores the unified diff (computed server-side) + the agent's reasoning
    + the prompt that triggered it."""
    if not file_path:
        return {"error": "file_path required"}
    if not (before or after):
        return {"error": "must provide before and/or after text"}
    import difflib
    from datetime import datetime
    target = dg
    if company_id and hub:
        cm = hub.get_company(company_id)
        if cm: target = cm.company_dg
    bef = (before or "").splitlines()
    aft = (after  or "").splitlines()
    diff_lines = list(difflib.unified_diff(
        bef, aft, fromfile=f"a/{file_path}", tofile=f"b/{file_path}",
        lineterm="", n=3))
    if len(diff_lines) > 200:
        diff_lines = diff_lines[:200] + [
            f"... [+{len(diff_lines) - 200} more lines truncated]"]
    diff_text = "\n".join(diff_lines) or "(no diff — files identical)"
    when = datetime.now().isoformat(timespec="seconds")
    citation = f"code-edit://{file_path}?at={when}"
    if line_range: citation += f"&lines={line_range}"
    if repo:        citation += f"&repo={repo}"

    question = f"[edit] {file_path}"
    if line_range: question += f" @ {line_range}"
    if prompt:     question += f' — "{prompt[:120]}"'

    reasoning_full = (
        f"AI edit captured at {when}; file={file_path}; "
        f"lines={line_range or '?'}; "
        f"reasoning={reasoning[:1500] or '(none)'}; "
        f"prompt={prompt[:500] or '(none)'}; "
        f"repo={repo or '?'}; citation={citation}")
    try:
        did = target.memory.store(
            question=question[:500],
            answer=diff_text[:6000],
            reasoning_summary=reasoning_full[:2500],
            communities_used=[], context_triples=[])
        try: target.memory.save()
        except Exception: pass
    except Exception as e:
        return {"error": f"store failed: {e}"}
    return {
        "decision_id":   did,
        "citation":      citation,
        "timestamp":     when,
        "lines_in_diff": len(diff_lines),
    }


async def _h_track_decision(dg, hub, dm, question: str = "",
                              answer: str = "", reasoning: str = "",
                              topic: str = "", company_id: str = None,
                              **_) -> dict:
    """Lightweight decision capture (no diff). Use for non-file work like
    'I decided X because Y'."""
    if not question or not answer:
        return {"error": "question and answer required"}
    from datetime import datetime
    target = dg
    if company_id and hub:
        cm = hub.get_company(company_id)
        if cm: target = cm.company_dg
    when = datetime.now().isoformat(timespec="seconds")
    tag = "[decision]"
    if topic: tag += f" {topic} ·"
    try:
        did = target.memory.store(
            question=f"{tag} {question}"[:500],
            answer=answer[:4000],
            reasoning_summary=(
                f"Logged at {when}; reasoning={reasoning[:1500] or '(none)'}; "
                f"topic={topic or '?'}")[:2500],
            communities_used=[], context_triples=[])
        try: target.memory.save()
        except Exception: pass
    except Exception as e:
        return {"error": f"store failed: {e}"}
    return {"decision_id": did, "timestamp": when}


async def _h_recent_code_edits(dg, hub, dm, limit: int = 20,
                                  file_path: str = "",
                                  company_id: str = None, **_) -> dict:
    """Most recent AI edit decisions, optionally filtered by file path."""
    target = dg
    if company_id and hub:
        cm = hub.get_company(company_id)
        if cm: target = cm.company_dg
    n = max(1, min(int(limit), 200))
    rows = target.memory.all_decisions()
    edits = []
    for r in rows:
        q = r.get("question") or ""
        if not q.startswith("[edit]") and not q.startswith("[edit:"):
            continue
        if file_path and file_path not in q:
            continue
        edits.append({
            "decision_id": r.get("id"),
            "summary":     q[:200],
            "diff":        (r.get("answer") or "")[:2000],
            "context":     (r.get("reasoning_summary") or "")[:600],
            "timestamp":   r.get("timestamp"),
        })
    edits.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return {"edits": edits[:n], "total_seen": len(edits)}


TOOL_HANDLERS = {
    "list_companies":      _h_list_companies,
    "query_knowledge":     _h_query_knowledge,
    "get_past_decisions":  _h_get_past_decisions,
    "store_decision":      _h_store_decision,
    "ingest_document":     _h_ingest_document,
    "get_stats":           _h_get_stats,
    "recall":              _h_recall,
    # ── AI-edit capture (Day-3 feature) ──
    "track_code_edit":     _h_track_code_edit,
    "track_decision":      _h_track_decision,
    "recent_code_edits":   _h_recent_code_edits,
    # ── GitHub repo → DG (Day-1/2 feature) ──
    "ingest_github":       _h_ingest_github,
    # ── Onboarding Companion (Day-5 feature) ──
    "get_onboarding_brief": _h_get_onboarding_brief,
    # ── Structural code graph (SQLite, sub-second) ──
    "find_callers":         _h_find_callers,
    "find_callees":         _h_find_callees,
    "blast_radius":         _h_blast_radius,
    "find_call_path":       _h_find_call_path,
    "causal_radius":        _h_causal_radius,
    "code_graph_stats":     _h_code_graph_stats,
    "rationales":           _h_rationales,
    "topology":             _h_topology,
    "suggested_questions":  _h_suggested_questions,
    "triage_pr":            _h_triage_pr,
    "update_files":         _h_update_files,
    "semantic_stale":       _h_semantic_stale,
    "get_context_pack":     _h_get_context_pack,
    "export_graph_html":    _h_export_graph_html,
    "export_graph_cypher":  _h_export_graph_cypher,
    "federated_callers":             _h_federated_callers,
    "federated_topology":            _h_federated_topology,
    "federated_rationale_search":    _h_federated_rationale_search,
}


async def invoke_tool(name: str, arguments: dict, dg, hub, dm) -> dict:
    """Dispatch a tool call. Used by both the MCP layer and the HTTP wrapper."""
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"Unknown tool: {name}"}
    try:
        return await handler(dg, hub, dm, **(arguments or {}))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ──────────────────────────────────────────────────────────────────────────────
# Stdio MCP server — only initialised when this module is run as main
# ──────────────────────────────────────────────────────────────────────────────
def _build_app(dg, hub, dm) -> Server:
    app = Server("decisiongraph")

    @app.list_tools()
    async def _list() -> list[types.Tool]:
        return TOOL_DEFS

    @app.call_tool()
    async def _call(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        result = await invoke_tool(name, arguments or {}, dg, hub, dm)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    return app


async def main():
    # initialise state at startup so module-level imports stay light
    dg = DecisionGraph()
    hub = EnterpriseHub()
    # DiscussionManager isn't strictly needed for MCP tools today but kept for parity
    from .discussion import DiscussionManager
    dm = DiscussionManager()

    app = _build_app(dg, hub, dm)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def _sync_main():
    """Console-script entry-point for `dg-mcp`."""
    asyncio.run(main())


if __name__ == "__main__":
    _sync_main()
