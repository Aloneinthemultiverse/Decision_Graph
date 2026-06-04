# DecisionGraph — Institutional Memory OS for AI Agents

A multi-tenant memory layer for AI agents, plus a complete agentic build platform on top.

This repo holds **three related-but-separable systems** and **two support services**.
They share storage so they compose, but each runs independently.

```
┌──────────────────────────────────────────────────────────────────┐
│  DG Mission Control (port 7000)                                  │
│  - Agentic OS: prompt -> working Next.js app                     │
│  - Dispatcher + 63 personas + 250 skills + 115 rules             │
│  - Live cinematic dashboard                                      │
└──────────────────────────────────────────────────────────────────┘
                              |
                              v  reads/writes
┌──────────────────────────────────────────────────────────────────┐
│  DecisionGraph (port 8000)                                       │
│  - FastAPI institutional-memory OS                               │
│  - Dual graph: NetworkX knowledge + tree-sitter code graph       │
│  - 30 MCP tools (Claude Code / Cursor / Antigravity can use)     │
│  - Multi-tenant per-visitor workspaces                           │
└──────────────────────────────────────────────────────────────────┘
                              |
                              v  uses
┌──────────────────────────────────────────────────────────────────┐
│  AgentNet (in-process)                                           │
│  - Sandboxed agent runtime                                       │
│  - Scoped grants, audit ledger, reputation                       │
└──────────────────────────────────────────────────────────────────┘

       Support services (optional, on-demand):
       ┌────────────────────────────┐  ┌───────────────────────────┐
       │  Mirofish (port 5001)      │  │  TimesFM (port 5002)      │
       │  Simulation studio backend │  │  Time-series forecasting  │
       └────────────────────────────┘  └───────────────────────────┘
```

---

## Table of contents
1. [The five services + ports](#1-the-five-services-and-the-ports-they-own)
2. [The three projects](#2-the-three-projects-one-line-each)
3. [Setup](#3-setup--once-per-machine)
4. [Running each service](#4-running-each-service--exact-commands)
5. [Using DecisionGraph (UI / Python / MCP)](#5-using-decisiongraph)
6. [Connecting to Claude Code, Cursor, Antigravity](#6-connecting-to-claude-code-cursor-antigravity)
7. [All 30 MCP tools (2 lines each)](#7-all-30-mcp-tools-2-lines-each)
8. [Using DG Mission Control](#8-using-dg-mission-control)
9. [Repository structure (what every file does)](#9-repository-structure-what-every-file-does)
10. [Storage layout on disk](#10-storage-layout-on-disk)
11. [Honest known issues](#11-honest-known-issues)
12. [License](#12-license)

---

## 1. The five services and the ports they own

| Service | Port | Process | Required for |
|---|---|---|---|
| **LLM proxy / gateway** | **8080** | `antigravity-claude-proxy` (`acc`) or any Anthropic-compatible gateway | every LLM call |
| **DecisionGraph** | **8000** | `python server.py` | Web UI, MCP, ingestion, memory |
| **DG Mission Control dashboard** | **7000** | `python dashboard_server.py` | the Mission Control UI + Dispatch button |
| **Mirofish (simulation)** | **5001** | `python mirofish_lite.py` | the Simulation Studio inside DG |
| **TimesFM (forecasting)** | **5002** | `python timesfm_service.py` | `/api/forecast` inside DG |

Each one is independent. Start only the ones the feature you use needs.

**Recommended bring-up order:**
1. **LLM proxy (8080)** — nothing works without it.
2. **DecisionGraph (8000)** — the memory layer.
3. **Dashboard (7000)** — the OS / Mission Control UI.
4. (Optional) **Mirofish (5001)** and **TimesFM (5002)**.

---

## 2. The three projects, one-line each

### DecisionGraph
**The Operating System for Institutional Memory.** FastAPI app + Python library. Ingest
PDFs / URLs / GitHub repos / decisions into a dual graph (semantic + structural),
queryable via REST or via MCP from any agent.

### AgentNet
**Sandboxed agent runtime.** Scoped, time-limited grants. Signed audit ledger. Agent
reputation. Inter-agent message bus. The substrate the OS uses so multiple agents can
write to a shared workspace safely.

### DG Mission Control
**Agentic OS that turns one prompt into a working full-stack app.** A dispatcher LLM
reads a 1,200-line master prompt, decomposes a task per feature, picks from 63 agent
personas + 250 skill modules + 115 rule sets, builds the site, self-corrects compile
errors, and ingests the result back into DG as long-term memory.

---

## 3. Setup — once per machine

### Prerequisites
* **Python 3.11+**.
* **Node 18+** (only if you plan to run DG Mission Control — built sites are Next.js).
* **Git**.
* An **LLM gateway** speaking the Anthropic Messages API on `http://localhost:8080`.
  Easiest path: `npm i -g antigravity-claude-proxy && acc start` (uses Google Gemini
  via your Google account).
* Optional: **Docker Desktop** if you want to use Postgres-backed apps built by the OS.

### Clone + Python deps
```bash
git clone https://github.com/Aloneinthemultiverse/Decision_Graph.git
cd Decision_Graph
pip install -r requirements.txt
cp .env.example .env       # then edit
```

### .env essentials
```env
LLM_BASE_URL=http://127.0.0.1:8080
LLM_API_KEY=dummy
LLM_MODEL=gemini-2.5-flash
```

Use **IPv4 `127.0.0.1`**, not `localhost`. On Windows `localhost` resolves to IPv6
first and Python's `httpx` will silently fail when the listener is IPv4-only.

---

## 4. Running each service — exact commands

### 4.1 LLM proxy on port 8080
```bash
npm i -g antigravity-claude-proxy
acc accounts add        # link a Google account (interactive OAuth)
acc start               # daemon on 0.0.0.0:8080
acc status              # confirm "Proxy is active"
```
Smoke test:
```bash
curl -X POST http://127.0.0.1:8080/v1/messages \
  -H "content-type: application/json" \
  -d '{"model":"gemini-2.5-flash","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
# expect HTTP 200
```

### 4.2 DecisionGraph on port 8000
```bash
# Windows
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
python server.py

# macOS / Linux
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python server.py
```
Open **http://localhost:8000** — landing page.
Open **http://localhost:8000/app** — Stitch UI (graph viewer, query, deep research).
API docs at **http://localhost:8000/swagger**.

> `PYTHONIOENCODING=utf-8` is mandatory on Windows. The background job worker
> prints a Unicode arrow during recovery; without UTF-8 the worker thread dies at
> boot and every queued job hangs forever.

### 4.3 DG Mission Control dashboard on port 7000
```bash
python dashboard_server.py
```
Open **http://localhost:7000**. Live build dashboard with the Dispatch button.
Requires proxy (8080); DG (8000) is optional but recommended (no DG = no memory).

### 4.4 Mirofish on port 5001
```bash
python mirofish_lite.py
```
DG's UI auto-detects Mirofish on 5001. Needed for Simulation Studio.

### 4.5 TimesFM on port 5002
```bash
python timesfm_service.py
```
Loopback only. Needed for `/api/forecast`.

### Quick "everything up" check
```bash
curl -s -o /dev/null -w "8080 %{http_code}\n" http://127.0.0.1:8080/v1/messages
curl -s -o /dev/null -w "8000 %{http_code}\n" http://127.0.0.1:8000/api/status
curl -s -o /dev/null -w "7000 %{http_code}\n" http://127.0.0.1:7000/
curl -s -o /dev/null -w "5001 %{http_code}\n" http://127.0.0.1:5001/health
curl -s -o /dev/null -w "5002 %{http_code}\n" http://127.0.0.1:5002/health
```

---

## 5. Using DecisionGraph

### 5.1 Web UI
**http://localhost:8000/app** — ingest documents, view the graph, query.

### 5.2 Python library
```python
from decisiongraph import DecisionGraph

dg = DecisionGraph(storage_dir="./storage/my_workspace")
dg.ingest("docs/architecture.pdf")
print(dg.query("What did we decide about authentication?"))
```

### 5.3 MCP server (stdio)
```bash
python -m decisiongraph.mcp_server
```
This is the entry point all three clients below connect to.

---

## 6. Connecting to Claude Code, Cursor, Antigravity

The MCP server is one stdio process. Each editor just needs to know how to launch it.

### 6.1 Claude Code

Add this to `~/.claude.json` (Windows: `C:\Users\<you>\.claude.json`):
```json
{
  "mcpServers": {
    "decisiongraph": {
      "command": "python",
      "args": ["-m", "decisiongraph.mcp_server"],
      "cwd": "C:/absolute/path/to/Decision_Graph",
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "DG_CODE_GRAPH_DB": "C:/absolute/path/to/Decision_Graph/storage/code_graph.db"
      }
    }
  }
}
```

Or via the CLI:
```bash
claude mcp add decisiongraph \
  --command python \
  --args "-m,decisiongraph.mcp_server" \
  --cwd /absolute/path/to/Decision_Graph \
  --env PYTHONIOENCODING=utf-8 \
  --env DG_CODE_GRAPH_DB=/absolute/path/to/Decision_Graph/storage/code_graph.db
```

Verify: `claude mcp list` → should show `decisiongraph: ✓ Connected`.

### 6.2 Cursor

Cursor reads from `~/.cursor/mcp.json` (or `~/.cursor-mcp.json` depending on
version). Add:
```json
{
  "mcpServers": {
    "decisiongraph": {
      "command": "python",
      "args": ["-m", "decisiongraph.mcp_server"],
      "cwd": "C:/absolute/path/to/Decision_Graph",
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "DG_CODE_GRAPH_DB": "C:/absolute/path/to/Decision_Graph/storage/code_graph.db"
      }
    }
  }
}
```
Restart Cursor. In the chat, type `@decisiongraph` to see the available tools.

### 6.3 Antigravity (Google's IDE)

Antigravity uses the same MCP-stdio config format. Add to its MCP config
(`File → Preferences → MCP Servers` or `~/.antigravity/mcp.json`):
```json
{
  "mcpServers": {
    "decisiongraph": {
      "command": "python",
      "args": ["-m", "decisiongraph.mcp_server"],
      "cwd": "C:/absolute/path/to/Decision_Graph",
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "DG_CODE_GRAPH_DB": "C:/absolute/path/to/Decision_Graph/storage/code_graph.db"
      }
    }
  }
}
```

Notes for **all three**:
* Use **absolute paths**, not `./` or `~`.
* Set `PYTHONIOENCODING=utf-8` on Windows or the server dies on Unicode.
* The proxy on 8080 must already be running before any tool calls hit the LLM.
* Same MCP server, same 30 tools — every editor gets the same capability set.

---

## 7. All 30 MCP tools (2 lines each)

Listed in the order DG exposes them.

| # | Tool | What it does |
|---|---|---|
| 1 | **list_companies** | Lists all company workspaces in DG. Call this first to discover the `company_id` values you can pass to every other tool. |
| 2 | **query_knowledge** | Semantic search over the knowledge graph for a question. Returns matched community ids + the graph triples that informed them; optionally scoped to a company. |
| 3 | **get_past_decisions** | Finds prior decisions similar to the current context. Always call this *before* making a new decision so the agent doesn't re-derive what's already known. |
| 4 | **store_decision** | Persists a decision (question + answer + reasoning) into DG memory. Optionally scoped to a company; otherwise lands in the personal store. |
| 5 | **ingest_document** | Ingests a PDF / TXT / MD / DOCX into the graph. Pick `target="knowledge"` for research/docs or `target="company"` for meeting notes / financials / policy. |
| 6 | **get_stats** | Returns graph and decision-memory statistics — node/edge counts, communities, decisions. Use to confirm the workspace actually has content. |
| 7 | **recall** | NO-LLM keyword recall: surfaces active, high-confidence decisions mentioning an entity (newest first) plus its graph neighbourhood. Sub-second; perfect pre-reasoning sanity check. |
| 8 | **find_callers** | Pure SQL on the code graph: who calls a given function/class? Returns caller file + qualified name + line numbers. Call this before any code change to scope the impact. |
| 9 | **find_callees** | Pure SQL on the code graph: what does this function call? Returns callees (with resolved targets and line numbers where known). |
| 10 | **blast_radius** | Walks the reverse call-graph + reverse imports N hops to answer "what FILES break if I change this one?". Grouped by hop distance. The killer feature for pre-edit safety. |
| 11 | **find_call_path** | BFS through the call graph from A to B. Returns the shortest call chain (or None). Use to understand how two functions are connected. |
| 12 | **causal_radius** | DG extension beyond blast_radius: combines structural impact with related decisions/PRs/ADRs. Tells you what breaks AND what the original intent was. |
| 13 | **code_graph_stats** | Raw counts in the structural graph — files / symbols / calls / imports / inherits / rationales. Use to verify the graph is populated. |
| 14 | **update_files** | Incrementally update the code graph after editing files — INSTANT, no clone, no re-ingest. Call this right after writing code so structural tools stay accurate. |
| 15 | **semantic_stale** | Lists files whose semantic nodes are stale (edited since the last semantic pass) and pending a lazy refresh. Use to know what's behind on indexing. |
| 16 | **get_context_pack** | Returns a tiered slice of the repo blueprint — small overview + per-file sections for the folders your task touches. Preload at session start instead of running a slow semantic query. |
| 17 | **rationales** | DG extension: surfaces inline `# WHY:` / `# HACK:` / `# SAFETY:` / `# TODO:` comments as first-class nodes linked to symbols. Tribal knowledge that usually dies in code review. |
| 18 | **topology** | God-nodes (chokepoints), surprising connections (unusual cross-folder edges), orphan symbols (likely dead code), deep inheritance chains. Pure SQL — no LLM cost. |
| 19 | **suggested_questions** | Auto-generated onboarding questions the graph is uniquely positioned to answer. Drop into the first prompt for a new joiner / new AI session. |
| 20 | **export_graph_html** | Exports an interactive HTML visualization of the code graph. Self-contained file. God-nodes in red, edges colored by confidence. Optional `focus_path` limits to one file + its neighbours. |
| 21 | **export_graph_cypher** | Exports the code graph as Neo4j Cypher `LOAD` statements. Use to drop the graph into a real graph DB for cross-team queries. |
| 22 | **federated_callers** | Finds callers of a symbol across MULTIPLE workspaces / repos. Pass `workspace_roots` (dirs containing `code_graph.db`); results are unioned. |
| 23 | **federated_topology** | Cross-repo god-nodes: symbols with the same leaf-name that are chokepoints in MULTIPLE repos. Org-wide refactor risk assessment in one call. |
| 24 | **federated_rationale_search** | Searches inline `# HACK:` / `# WHY:` / `# SAFETY:` rationale comments across all federated repos. Useful for finding "is this gotcha repeated elsewhere?". |
| 25 | **triage_pr** | DG extension: PR risk scoring. Pass changed files; get blast radius + god-node touches + rationale warnings + a 0–100 risk score + merge-order hint. |
| 26 | **get_onboarding_brief** | One-shot "what to know" bundle for a new joiner or new AI session: overview, active topics, recent decisions, recently-edited files, who's been editing. Ready-to-paste `brief_text` included. |
| 27 | **ingest_github** | Clones a public github.com repo, runs tree-sitter AST chunking, summarises every file/folder with the LLM, builds a call graph, captures recent PRs, and uses an incremental hash cache so re-runs only re-process changed files. |
| 28 | **track_code_edit** | Captures a code edit the agent just made. Pass before+after text; the platform computes the diff and stores it as a permanent decision tagged WHO/WHEN/WHERE/WHY. Call after every meaningful change. |
| 29 | **track_decision** | Records an arbitrary decision that did NOT involve a code edit — tech-stack choice, design call, scoping decision. Becomes a permanent decision node. |
| 30 | **recent_code_edits** | Returns the N most recent AI-driven code edits in this workspace. Use for audit, change history, and giving the next agent context about what's been touched lately. |

---

## 8. Using DG Mission Control

After proxy (8080) and dashboard (7000) are up, open **http://localhost:7000**, type
your task into the Launch screen, click **Dispatch ▶**.

Or CLI:
```bash
DISPATCH_MODEL=gemini-2.5-flash BUILD_MODEL=gemini-2.5-flash \
  python os_build.py "Build a website for a tea house named Steep — hero, menu, gallery, reservation form."
```
What happens:
1. **Dispatcher** reads `ecc/DISPATCHER_PROMPT.md`, decomposes the task per feature,
   picks personas/skills/rules per subtask.
2. **Builder** executes each subtask AS the chosen persona, applying its skills.
3. **Boot gate** runs `npm install`, auto-installs missing dependencies, runs
   `tsc --noEmit`, and dispatches `build-error-resolver` until it compiles.
4. **DG ingest** pushes the finished project's blueprint + code graph into DG as
   long-term memory.
5. **Dashboard** streams the whole thing live — DAG, agent logs, graph fill.

Built sites land in `.tmp/os_site/`. To run one:
```bash
cd .tmp/os_site
npm install
npm run dev    # serves on http://localhost:3000
```

---

## 9. Repository structure (what every file does)

```
Decision_Graph/
├── server.py                       # FastAPI app — DG web UI + REST + MCP-over-HTTP (port 8000)
├── mirofish_lite.py                # Simulation Studio backend (port 5001)
├── timesfm_service.py              # TimesFM forecasting service (port 5002)
├── os_build.py                     # DG Mission Control CLI entry: prompt -> built app
├── dashboard_server.py             # Mission Control dashboard server (port 7000)
├── gen_catalog.py                  # Builds ecc/TASK_CATALOG.md from agents/skills/etc.
├── requirements.txt
├── .env.example
├── ARCHITECTURE.md                 # System architecture deep-dive
├── DOCUMENTATION.md                # 25k+ lines of detail on every subsystem
├── SETUP_CLAUDE_CODE.md            # Claude Code MCP setup walkthrough
├── BACKUP_RESTORE.md               # Backup / restore procedure for storage/
├── PRODUCTION_READY.md             # Production deploy checklist
├── ENTERPRISE_OS_PLAN.md           # Enterprise-scale architecture plan
├── UI_SPEC.md                      # Stitch UI specification
│
├── decisiongraph/                  # ─── DG core package ──────────────────────────
│   ├── core.py                     # DecisionGraph class — ingest, query, save/load
│   ├── graph.py                    # entity resolution, Louvain communities, summaries
│   ├── decisions.py                # decision provenance, confidence, decay, supersede
│   ├── consolidate.py              # dream cycle — community redetect + wisdom promotion
│   ├── ingest.py                   # parallel triple extraction + checkpoint resume
│   ├── ingest_sources.py           # PDF / URL / YouTube / media handlers
│   ├── document_handlers.py        # PDF/DOCX/MD parsers
│   ├── codebase.py                 # GitHub-repo blueprint + ingest pipeline
│   ├── codebase_ast.py             # tree-sitter AST helpers
│   ├── codebase_local.py           # local-folder counterpart of ingest_github_url_v2
│   ├── code_graph.py               # tree-sitter code graph (SQLite)
│   ├── code_graph_cli.py
│   ├── code_graph_federated.py     # cross-repo federation
│   ├── code_graph_viz.py           # graph viz exports (HTML / Cypher)
│   ├── query.py                    # ReAct deep-query mode
│   ├── reviews.py                  # PR triage + risk scoring
│   ├── company.py                  # multi-tenant company hub
│   ├── companies.py
│   ├── projects.py                 # per-device project registry
│   ├── workspace.py                # per-visitor workspace isolation
│   ├── listings.py                 # marketplace listings
│   ├── sellers.py                  # marketplace seller registry
│   ├── jobs.py                     # durable SQLite job queue
│   ├── config.py
│   ├── logging_setup.py
│   ├── orchestration.py            # task orchestration helpers
│   ├── dream.py                    # legacy dream-cycle entry
│   ├── discussion.py               # multi-turn discussion sessions
│   ├── evals_capture.py            # eval set replay
│   ├── forecasting.py              # TimesFM client wrappers
│   ├── simulation.py               # Mirofish client wrappers
│   ├── mcp_server.py               # stdio MCP server — exposes 30 tools
│   ├── mcp_tools.py                # tool implementations
│   ├── mcp_gateway.py
│   ├── mcp_http.py                 # MCP over HTTP variant
│   ├── integrations/               # ── DG integrations ────────────
│   │   ├── base.py
│   │   ├── slack.py
│   │   ├── jira.py
│   │   ├── notion.py
│   │   ├── confluence.py
│   │   ├── sharepoint.py
│   │   ├── google.py
│   │   └── supabase_out.py
│   ├── agent.py                    # ── AgentNet pieces ────────────
│   ├── agent_access.py             # scoped grants
│   ├── agent_catalog.py
│   ├── agent_crypto.py
│   ├── agent_job.py
│   ├── agent_messages.py           # inter-agent message bus
│   ├── agent_network.py            # AgentNet registry
│   ├── agent_reputation.py
│   ├── agent_sandbox.py
│   └── (DG Mission Control modules — see below)
│       ├── kernel.py               # the 4-job kernel (DISPATCH / LOAD / GRANT / RUN)
│       ├── kernel_cag.py           # hot-loop session scratchpad
│       ├── dispatcher.py           # LLM-driven dispatcher (reads ecc/DISPATCHER_PROMPT.md)
│       ├── scaffold.py             # OS-owned stack templates (Next.js skeleton)
│       ├── llm_executor.py         # builder executor: persona + skills -> writes files
│       ├── run_state.py            # writes the live run_state.json for the dashboard
│       ├── catalog.py              # reads ecc/TASK_CATALOG.md
│       ├── context_pack.py         # tiered blueprint slicer
│       ├── dg_sync.py              # OS -> DG decision sync (via /api/mcp/call/track_decision)
│       └── dg_local_ingest.py      # OS -> DG knowledge-graph ingest (local folder variant)
│
├── ecc/                            # ─── ECC capability catalog ─────────
│   ├── DISPATCHER_PROMPT.md        # 1,200-line master prompt
│   ├── TASK_CATALOG.md             # auto-generated capability menu
│   ├── agents/                     # 63 agent personas
│   ├── skills/                     # 250 skill modules
│   ├── commands/                   # 79 commands
│   ├── rules/                      # 115 rule sets
│   ├── hooks/
│   ├── contexts/
│   ├── schemas/
│   └── integrations/
│
├── agentnet/                       # ─── AgentNet (extended runtime) ───
│   ├── agents/                     # agent definitions (markdown)
│   ├── hooks/
│   ├── ui/
│   ├── agent_catalog.json
│   ├── README.md
│   └── IDE_INTEGRATION.md
│
├── dashboard/                      # ─── Mission Control web dashboard ──
│   ├── index.html                  # the cinematic dashboard SPA
│   ├── catalog.json                # cached catalog for the UI
│   └── run_state.json              # live OS run state (written by run_state.py)
│
├── stitch_ui/                      # ─── DG Web UI (served by server.py) ──
│   ├── index.html
│   ├── landing.html
│   ├── knowledge_base.html
│   ├── graph_visualizer.html
│   ├── query_mode.html
│   └── discussion_chat.html
│
├── tests/                          # pytest suites
├── storage/                        # runtime data (gitignored)
└── .tmp/                           # build artifacts, last OS site, logs
```

---

## 10. Storage layout on disk

```
storage/
  workspaces/<token>/personal/      # per-visitor DG workspace
    graph_clean.pkl                  # NetworkX knowledge graph
    decision_graph.pkl               # decision memory
    code_graph.db                    # SQLite code graph
    communities_clean.pkl
    summaries_clean.pkl
    checkpoint_<doc>.pkl             # resumable ingest checkpoints
    blueprints/                      # generated repo blueprints
  companies/                         # multi-tenant company memory
  projects.json                      # device -> projects registry
  jobs.db                            # durable job queue
  dg_settings.json                   # global settings

.tmp/
  os_site/        # the last app the OS built
  os_run.log      # last OS build log
  dash_launch.log # last Dispatch-button launch log
```

Backup / restore in `BACKUP_RESTORE.md`.

---

## 11. Honest known issues

* **Windows `cp1252` console** — set `PYTHONIOENCODING=utf-8` before `python server.py`,
  otherwise the job worker thread dies on a Unicode character at boot.
* **`localhost` vs `127.0.0.1`** — on Windows, Python's `httpx` resolves `localhost` to
  IPv6 first. Use `127.0.0.1` in `LLM_BASE_URL`.
* **Per-workspace cache** — the FastAPI server caches each workspace's DG instance in
  memory. If you write to the workspace pickle from outside the server, the running
  server keeps serving the stale in-memory copy until restart. Dashboard's Graph
  Viewer bypasses this by reading `graph_clean.pkl` directly off disk.
* **`entity_resolution()` AxisError on tiny graphs** — guard with `len(nodes) < 2` if
  you hit it.
* **LLM proxy stability under load** — the antigravity proxy can flap under heavy
  concurrent load with only 2 Google accounts; the OS has retry-with-backoff for
  this, but if you're hammering it consider adding more accounts.

---

## 12. License

See `LICENSE`.
