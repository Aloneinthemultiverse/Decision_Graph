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


TOOL_HANDLERS = {
    "list_companies":    _h_list_companies,
    "query_knowledge":   _h_query_knowledge,
    "get_past_decisions": _h_get_past_decisions,
    "store_decision":    _h_store_decision,
    "ingest_document":   _h_ingest_document,
    "get_stats":         _h_get_stats,
    "recall":            _h_recall,
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


if __name__ == "__main__":
    asyncio.run(main())
