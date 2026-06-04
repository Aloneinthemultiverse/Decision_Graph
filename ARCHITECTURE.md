# Complete Architecture — Enterprise Agentic OS

*The full materials map: everything that exists today, before we build the OS.*

---

## 0. The whole picture in one diagram

```
╔══════════════════════════════════════════════════════════════════╗
║                     ENTERPRISE AGENTIC OS                         ║
║                                                                    ║
║   ┌────────────── KERNEL (TO BUILD) ──────────────┐               ║
║   │  dispatch → load → grant → run+log             │               ║
║   │  + agentic-RAG (structural) + CAG (semantic)   │               ║
║   └────────────────────────────────────────────────┘               ║
║          │              │               │                          ║
║          ▼              ▼               ▼                          ║
║   ┌──────────┐   ┌──────────────┐   ┌──────────────────┐          ║
║   │  AGENTS  │   │   MEMORY      │   │   GOVERNANCE      │          ║
║   │  (ECC)   │   │   (DG core)   │   │   (DG AgentNet)   │          ║
║   │ REUSE    │   │   HAVE        │   │   HAVE            │          ║
║   └──────────┘   └──────────────┘   └──────────────────┘          ║
║                                                                    ║
║   transport: MCP (stdio + HTTP gateway)   UI: stitch_ui + agentnet/ui ║
╚══════════════════════════════════════════════════════════════════╝
```

---

## 1. MEMORY (DG core) — what an agent remembers

### 1a. Knowledge graph (semantic) — SLOW (~13s cold), CAG this
| Module | Role |
|---|---|
| `core.py` | `DecisionGraph` — the main object; ingest + query + lazy graph load |
| `graph.py` | build graph, entity resolution, Louvain communities, summaries |
| `query.py` | beam query, community embeddings, semantic retrieval |
| `ingest.py` | triple extraction (LLM), chunking, graph build/merge |
| `ingest_sources.py` | source adapters |
| `document_handlers.py` | read PDF/DOCX/MD/TXT |
| `codebase.py` | github ingest → blueprint (the CAG blob) + README/commits/PRs |
| `codebase_ast.py` | tree-sitter parsing, 32 langs, imports/rationales/param-types |

### 1b. Code graph (structural) — FAST (~10ms), agentic-RAG this
| Module | Role |
|---|---|
| `code_graph.py` | SQLite: symbols, calls, imports, inherits, rationales; find_callers, blast_radius, triage_pr, topology, **update_files**, semantic_stale |
| `code_graph_cli.py` | `rebuild` + `install-hook` (auto-rebuild on commit) |
| `code_graph_federated.py` | cross-repo queries (federation) |
| `code_graph_viz.py` | HTML viz + Neo4j Cypher export |

### 1c. Decision memory — the "why" layer
| Module | Role |
|---|---|
| `decisions.py` | `DecisionMemory` — store/recall decisions, auto-link, confidence decay, supersede, timeline |
| `discussion.py` | multi-message sessions, folded into memory on end |

### 1d. Higher-order memory
| Module | Role |
|---|---|
| `dream.py` | **Dream Cycle** — background distillation (← reuse for instinct distillation, Phase 4) |
| `evals_capture.py` | eval capture + replay |
| `forecasting.py` | time-series (TimesFM + fallback) |
| `simulation.py` | multi-persona decision simulation (mirofish) |

### Memory delivery (the OS layer)
- **Structural** (find_callers, blast_radius...) → **agentic RAG** (agent pulls on demand, fast)
- **Semantic** (blueprint, query) → **CAG** (preload once, slow otherwise)
- **Two-tier CAG:** KERNEL-CAG (hot, this session) + DG-CAG (stable blueprint)

---

## 2. GOVERNANCE (DG AgentNet) — who can touch what

| Module | Role |
|---|---|
| `agent_access.py` | scoped access grants for external agents (the bearer tokens) |
| `agent_sandbox.py` | the sandbox (network-less run_python) |
| `agent_job.py` | end-to-end agent job lifecycle |
| `agent_crypto.py` | at-rest encryption for grant/audit files |
| `agent_network.py` | cross-company network directory |
| `agent_reputation.py` | reputation from the immutable audit log |
| `agent_messages.py` | run-scoped message bus for multi-agent dialog |
| `company.py` | `EnterpriseHub`, `CompanyMemory` — multi-tenant isolation |
| `workspace.py` | per-visitor workspace isolation |
| `projects.py` | named workspaces per device |

### Marketplace (already scaffolded — feeds Phase 6)
| Module | Role |
|---|---|
| `listings.py` | third-party agent listings |
| `sellers.py` | sellers who list agents |
| `reviews.py` | buyer feedback on listings |
| `orchestration.py` | multi-agent orchestration (Phase 3 of AgentNet) |

---

## 3. TRANSPORT — how agents reach memory + tools

| Module | Role |
|---|---|
| `mcp_server.py` | **stdio MCP** — 29 tools (Claude Code local). `TOOL_HANDLERS` |
| `mcp_http.py` | **HTTP MCP gateway** — bearer-auth, scope-gated |
| `mcp_tools.py` | HTTP gateway tool registry (56 tools incl. our structural set) |
| `mcp_gateway.py` | gateway plumbing |
| `jobs.py` | durable async job queue (ingest_github etc. run here) |

### Two MCP paths
- **stdio** (`mcp_server.py`): local Claude Code, full owner tools, ~5s boot
- **HTTP** (`mcp_http.py` + `mcp_tools.py`): remote/scoped agents via ngrok, bearer token, audited

---

## 4. AGENTS (ECC — REUSE, do not build)
- `agents/` — 63 specialist agents (planner, reviewer, security...)
- `skills/` — 249 workflow playbooks
- `hooks/` — session-start/end, pre-compact, evaluate-session (← Phase 1 seams edit these)
- `rules/` — always-on guidelines
- ECC's own instinct system (continuous-learning-v2) → we redirect its output into DG

### DG's own agents (agentnet/agents/) — existing, smaller set
- dialog_agent, knowledge_qa, summarizer, topic_counter, mesh_provider, __react__

---

## 5. UI
| Where | Pages |
|---|---|
| `stitch_ui/` | landing, index, knowledge_base, query_mode, graph_visualizer, discussion_chat |
| `agentnet/ui/` | dashboard, agents, marketplace, orchestration, projects, sellers, onboarding, docs, listing_profile |
| `server.py` | serves all of it + REST API + MCP HTTP routes |

---

## 6. What the OS adds on top (the BUILD)

| Piece | Built on | Phase |
|---|---|---|
| Integration seams (ECC↔DG) | ECC hooks + DG MCP | 1 |
| `get_context_pack` (tiered blueprint slice) | codebase.py blueprint | 2 |
| KERNEL-CAG (hot session scratchpad) | new | 2 |
| Kernel (dispatch/load/grant/run+log) | router + DG + AgentNet | 3 |
| Consolidation + instinct distillation | dream.py + decisions.py | 4 |
| RBAC / SSO / admin dashboard | company.py + new | 5 |
| Unified console + marketplace | stitch_ui + listings/sellers | 6 |

---

## 7. Data stores (where state physically lives)
```
storage/
  workspaces/<ws_id>/personal/
      decision_graph.pkl        ← knowledge graph + decisions (semantic)
      code_graph.db             ← structural code graph (SQLite)
      blueprints/<repo>.md      ← the CAG blob
      communities_clean.pkl, summaries_clean.pkl, graph_clean.pkl
      discussions/sessions.pkl
  companies/<company_id>/        ← multi-tenant company graphs
  agent_audit.jsonl             ← immutable audit log (governance)
  agents.json, grants           ← agent access state
  jobs.db                       ← async job queue
<repo>/.dg_code_graph.db         ← per-repo structural graph (hook-maintained)
```

---

## 8. Honest gaps (what's NOT there yet)
- No kernel (the coordinator) — Phase 3
- No two-tier CAG wiring — Phase 2
- No ECC↔DG seams — Phase 1
- Instinct distillation exists in spirit (dream.py) but not wired to code facts — Phase 4
- No team RBAC / SSO / admin console — Phase 5
- ECC not yet installed alongside — Phase 0
