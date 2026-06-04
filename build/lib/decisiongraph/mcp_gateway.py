"""Per-job MCP gateway: the scope gate + audit for every tool call an agent
makes. Speaks JSON-RPC 2.0 (MCP-shape). Used by the sandbox stdio bridge.

This is what makes "the agent reaches the knowledge graph via MCP" literally
true — every read goes through here, every call hits the Step-1 access gate,
every call lands in the immutable audit log.

Tools exposed to sandboxed agents (read-only by default):
  * recall(topic)               -> decisions + graph triples + summaries in scope
  * scoped_search(topic, query) -> same scope, ranked against `query` (BM25-ish)
  * done(answer, citation?)     -> agent signals end + final answer

Notes:
  - Every tools/call requires a `topic` argument. The gate checks it against
    the agent's allowlist. No topic-less data calls.
  - Stdlib only on the gate side. Pure functions over a MemoryAdapter +
    AgentAccessStore; trivially unit-testable without docker / network.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from typing import Any, Optional

from .agent_access import AgentAccessStore, AgentAccessError


_STOP = set("the a an of and or to in on for with by is are was were be been "
            "being it its this that those these as at from into about over "
            "i you he she we they our your their what which who whose how "
            "why when where do does did will would can could should "
            "have has had not no yes if then else there here".split())


def _tok(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in _STOP and len(w) > 1]


def _err(req_id, code, message, data=None):
    e = {"jsonrpc": "2.0", "id": req_id,
         "error": {"code": code, "message": message}}
    if data is not None:
        e["error"]["data"] = data
    return e


def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


TOOL_SCHEMA = [
    {"name": "recall",
     "description": "Return every piece of evidence in scope for `topic` "
                    "(decisions + graph triples + community summaries).",
     "inputSchema": {"type": "object",
                     "properties": {"topic": {"type": "string"}},
                     "required": ["topic"]}},
    {"name": "scoped_search",
     "description": "Like recall, but ranks evidence against `query` and "
                    "returns the top N hits.",
     "inputSchema": {"type": "object",
                     "properties": {"topic": {"type": "string"},
                                    "query": {"type": "string"},
                                    "k": {"type": "integer", "default": 5}},
                     "required": ["topic", "query"]}},
    {"name": "ask",
     "description": "Ask the company's DecisionGraph a natural-language "
                    "question. Runs the host-side ReAct loop (THINK/ACT/"
                    "ANSWER) using the company's LLM gateway against ONLY "
                    "the scoped slice. Returns a multi-paragraph, "
                    "graph-grounded answer with reasoning.",
     "inputSchema": {"type": "object",
                     "properties": {"topic": {"type": "string"},
                                    "question": {"type": "string"}},
                     "required": ["topic", "question"]}},
    {"name": "done",
     "description": "Signal job completion. Pass your final `answer` and an "
                    "optional `citation`. After this the sandbox exits.",
     "inputSchema": {"type": "object",
                     "properties": {"answer": {"type": "string"},
                                    "citation": {"type": "string"}},
                     "required": ["answer"]}},
]


class MCPGateway:
    """One gateway instance per job. Holds the bound token + adapter."""

    def __init__(self, store: AgentAccessStore, adapter, agent_token: str,
                 job_id: str, dg=None, owner: bool = False,
                 ws=None, ctx: Optional[dict] = None):
        self.store = store
        self.adapter = adapter        # has .fetch_scope(topic) -> list[dict]
        self.dg = dg                  # workspace DecisionGraph (for `ask`/react)
        self.token = agent_token
        self.job_id = job_id
        self.final_answer: Optional[dict] = None
        self.call_count = 0
        self.owner = owner            # owner-mode = full tool surface
        self.ws = ws                  # workspace object (for full-tool dispatch)
        self.ctx = ctx or {}          # JOBS, S_simulator, etc.

    # ── tool implementations (after scope gate) ─────────────────────────
    def _recall(self, topic: str) -> dict:
        evidence = self.adapter.fetch_scope(topic)
        return {"topic": topic, "evidence": evidence,
                "count": len(evidence)}

    def _scoped_search(self, topic: str, query: str, k: int = 5) -> dict:
        evidence = self.adapter.fetch_scope(topic)
        q_set = set(_tok(query))
        if not q_set:
            return {"topic": topic, "query": query, "hits": evidence[:k]}

        # tiny BM25-ish ranker over scoped evidence
        df: Counter = Counter()
        docs: list[list[str]] = []
        for e in evidence:
            t = _tok(self._evidence_text(e))
            docs.append(t)
            for w in set(t):
                df[w] += 1
        N = max(1, len(docs))
        scored = []
        for e, toks in zip(evidence, docs):
            tf = Counter(toks)
            score = 0.0
            for w in q_set:
                if w in tf:
                    idf = math.log((N + 1) / (df[w] + 1)) + 1
                    score += (1 + math.log(tf[w])) * idf
            scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return {"topic": topic, "query": query,
                "hits": [e for s, e in scored[:k] if s > 0]}

    @staticmethod
    def _evidence_text(e: dict) -> str:
        if e.get("kind") == "decision":
            return f"{e.get('question','')} {e.get('answer','')}"
        if e.get("kind") == "triple":
            return f"{e.get('subject','')} {e.get('relation','')} {e.get('object','')}"
        if e.get("kind") == "summary":
            return e.get("summary", "")
        return ""

    # ── the gate ─────────────────────────────────────────────────────────
    def handle(self, req: dict) -> dict:
        """Process a single JSON-RPC 2.0 request and return its response."""
        rid = req.get("id")
        method = req.get("method", "")

        if method == "initialize":
            return _ok(rid, {"protocolVersion": "2025-06-18",
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "decisiongraph-gateway",
                                            "version": "1.0"}})
        if method == "tools/list":
            tools = list(TOOL_SCHEMA)
            try:
                from .mcp_tools import list_tools as _list_full
                tools.extend(_list_full(owner=self.owner))
            except Exception:
                pass
            return _ok(rid, {"tools": tools})

        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            self.call_count += 1

            if name == "done":
                answer = str(args.get("answer", ""))[:8000]
                citation = str(args.get("citation", ""))[:500]
                self.final_answer = {"answer": answer, "citation": citation}
                self.store.record_event("mcp_done", job_id=self.job_id,
                                        token=self.token,
                                        agent_name=self._agent_name(),
                                        calls=self.call_count)
                return _ok(rid, {"content": [{"type": "text",
                                              "text": "ok"}]})

            # which tools actually need a topic? the legacy in-process
            # data tools (recall/scoped_search/ask) always do. Registry tools
            # have `needs_scope` declared. Owner tokens never need a topic.
            legacy_scoped = name in ("recall", "scoped_search", "ask")
            registry_needs_scope = False
            try:
                from .mcp_tools import TOOLS as _REG
                if name in _REG:
                    registry_needs_scope = bool(_REG[name].get("needs_scope"))
            except Exception:
                pass
            requires_topic = (legacy_scoped or registry_needs_scope) and not self.owner
            topic = str(args.get("topic", "")).strip()
            if requires_topic and not topic:
                self._log_denied(name, topic, "missing topic argument")
                return _err(rid, -32602,
                            "tool requires a 'topic' argument for scope check")

            # the gate (logs allow/deny) — only runs for tools that NEED a
            # topic scope; non-scoped tools (send_message, list_peers,
            # get_graph, run_python, …) skip the gate. Owner tokens always
            # skip.
            if not self.owner and (legacy_scoped or registry_needs_scope):
                try:
                    self.store.authorize(self.token, topic, "read")
                except AgentAccessError as e:
                    return _err(rid, -32099,
                                f"scope check failed: {e}",
                                data={"topic": topic, "tool": name})
            else:
                self.store.record_event(
                    "mcp_call", job_id=self.job_id, token=self.token,
                    agent_name=self._agent_name(),
                    tool=name, topic=topic, owner=self.owner)

            if name == "recall":
                payload = self._recall(topic)
            elif name == "scoped_search":
                payload = self._scoped_search(
                    topic, str(args.get("query", "")),
                    int(args.get("k", 5) or 5))
            elif name == "ask":
                question = str(args.get("question", "")).strip()
                if not question:
                    return _err(rid, -32602, "'ask' requires a 'question' argument")
                if self.dg is None:
                    return _err(rid, -32603, "react tool unavailable: no DG bound")
                try:
                    from .agent import react_agent
                    answer = react_agent(
                        question=question, client=self.dg.client,
                        G=self.dg.G,
                        community_summaries=self.dg.summaries,
                        community_ids=self.dg.community_ids,
                        community_embeddings=self.dg.community_embeddings,
                        embed_model=self.dg.embed_model,
                        memory=self.dg.memory)
                    payload = {"topic": topic, "question": question,
                               "answer": answer or "",
                               "citation": "ReAct reasoning over the knowledge graph"}
                except Exception as e:
                    return _err(rid, -32099, f"react agent failed: {e}")
            else:
                # dispatch to the full registry (full DG tool surface).
                # ws may be None for dialog-mode agents that only use ctx-
                # based tools (post_message, list_peers, etc.); per-tool
                # handlers fail cleanly if they actually need ws.
                try:
                    from .mcp_tools import call_tool as _call_full
                    payload = _call_full(name, self.ws, args, self.ctx,
                                          owner=self.owner)
                except Exception as e:
                    return _err(rid, -32099, f"tool failed: {e}")
                # Only treat as an error if the error field is non-empty.
                # Job records legitimately have error=None for success cases.
                if isinstance(payload, dict) and payload.get("error"):
                    return _err(rid, -32099, payload["error"])
                self.store.record_event("mcp_call",
                    job_id=self.job_id, token=self.token,
                    agent_name=self._agent_name(),
                    tool=name, owner=self.owner)
                import json as _j
                return _ok(rid, {"content": [{"type": "text",
                                              "text": _j.dumps(payload)}]})

            self.store.record_event(
                "mcp_call", job_id=self.job_id, token=self.token,
                agent_name=self._agent_name(),
                tool=name, topic=topic,
                returned=len(payload.get("evidence")
                             or payload.get("hits") or []))
            # MCP tools/call result shape: {content: [{type:"text", text:..}]}
            import json as _j
            return _ok(rid, {"content": [{"type": "text",
                                          "text": _j.dumps(payload)}]})

        return _err(rid, -32601, f"unknown method: {method}")

    def _agent_name(self) -> str:
        g = self.store.get(self.token)
        return (g or {}).get("agent_name", "unknown")

    def _log_denied(self, tool, topic, reason):
        self.store.record_event(
            "mcp_denied", job_id=self.job_id, token=self.token,
            agent_name=self._agent_name(),
            tool=tool, topic=topic, reason=reason)
