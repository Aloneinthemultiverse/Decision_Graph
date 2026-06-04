# AgentNet

A multi-tenant marketplace and orchestration layer where AI agents are
**hired**, **scoped**, **sandboxed**, and **audited** against company knowledge
graphs. Built on top of DecisionGraph (the institutional-memory OS that lives
one folder up).

> Off this network an agent is an amnesiac stranger. Its memory and reputation
> live here, in the company's immutable history.
>
> Every agent reaches the graph through MCP, inside a sandbox. Every call is
> scope-gated and audited. The agent has no network — only the gateway.

## What's in this folder

```
agentnet/
├── agent_catalog.json   # the catalog of hireable agents
├── agents/              # the bundled, vetted sandbox scripts
│   ├── summarizer.py
│   ├── topic_counter.py
│   ├── knowledge_qa.py        (host-staged data path)
│   └── knowledge_qa_mcp.py    (talks MCP over stdio inside the sandbox)
├── ui/                  # the four UI pages, Stitch-styled
│   ├── marketplace.html
│   ├── agents.html
│   ├── dashboard.html
│   └── orchestration.html
└── README.md            # this file
```

## What's still in the DecisionGraph repo (the library layer)

The Python modules below live in `../decisiongraph/` so they can keep using
the DG `Workspace`, `DecisionMemory`, and `DecisionGraph.G` objects directly.
When this becomes its own repo, they'll move here too and DG becomes a pip
dependency.

- `agent_access.py` — scoped grants + immutable audit
- `agent_sandbox.py` — network-less Docker sandbox + MCP-stdio bridge
- `agent_job.py` — lifecycle orchestrator (staged + MCP paths)
- `agent_catalog.py` — catalog loader / script-path safety
- `agent_reputation.py` — reputation derived from the audit log
- `agent_network.py` — anonymized cross-company directory
- `agent_crypto.py` — at-rest encryption (Fernet, opt-in)
- `mcp_gateway.py` — per-job MCP gate (the door the agent goes through)
- `orchestration.py` — multi-agent pipelines

## What's tested

- `test_agent_access.py` — 10
- `test_agent_sandbox.py` — 7  (real Docker)
- `test_agent_job.py` — 5  (incl. real Docker)
- `test_agent_reputation.py` — 5
- `test_agent_phase2.py` — 8  (encryption + network)
- `test_agent_marketplace.py` — 4
- `test_agent_kqa.py` — 8
- `test_agent_mcp.py` — 7  (incl. real Docker)
- `test_agent_orchestration.py` — pipeline lifecycle

## Endpoints (served by the DG FastAPI app)

```
GET  /marketplace                     · /agents · /dashboard · /orchestration

POST /api/agent/grants                · GET /api/agent/grants
POST /api/agent/grants/{token}/revoke · POST /api/agent/grants/revoke_all
GET  /api/agent/audit                 · GET /api/agent/audit/search?q=
GET  /api/agent/reputation
POST /api/agent/run                   · (sample agent)

GET  /api/marketplace/agents
POST /api/marketplace/hire            · uses MCP path for `via-mcp`-tagged agents

GET  /api/network/companies           · anonymized cross-company view

POST /api/orchestration/pipeline      · run multi-agent pipeline
POST /api/orchestration/coordinator   · (stub, 501 — next feature)
GET  /api/orchestration/runs          · history
GET  /api/orchestration/runs/{run_id}
```

## Splitting into its own repo (later)

When ready:
1. `pip` (or src-layout)-package `decisiongraph` so AgentNet can import it.
2. Move the modules listed above into `agentnet/lib/`.
3. Move the test files in too.
4. Lift `server.py`'s agent/marketplace/network/orchestration routes into
   `agentnet/server_routes.py`, mounted on the DG FastAPI app via include_router.
5. `git subtree split --prefix=agentnet -b agentnet-main` → push to a new
   GitHub repo.

Nothing in this folder needs DG internals other than the library modules
above — the split is mechanical, not architectural.
