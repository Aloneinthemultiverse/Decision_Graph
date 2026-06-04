# DecisionGraph — Institutional Memory OS for AI Agents

A multi-tenant memory layer for AI agents, plus a complete agentic build platform on top.

This repo holds three related-but-separable systems and two support services. They share
storage so they compose, but each runs independently.

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
│  - 30+ MCP tools (Claude Code / Cursor can query the DB)         │
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

## The five services (and the ports they own)

| Service | Port | Process | Required for |
|---|---|---|---|
| **LLM proxy / gateway** | **8080** | `antigravity-claude-proxy` (`acc`) or any Anthropic-compatible gateway | every LLM call |
| **DecisionGraph** | **8000** | `python server.py` | UI, MCP, ingestion, memory |
| **DG Mission Control dashboard** | **7000** | `python dashboard_server.py` | the Mission Control UI + Dispatch button |
| **Mirofish (simulation)** | **5001** | `python mirofish_lite.py` | the Simulation Studio inside DG |
| **TimesFM (forecasting)** | **5002** | `python timesfm_service.py` | `/api/forecast` inside DG |

Each one is independent. You only need to start the ones the feature you're using actually depends on.

**Recommended bring-up order:**
1. **LLM proxy (8080)** first — nothing works without an LLM endpoint.
2. **DecisionGraph (8000)** — the memory layer.
3. **Dashboard (7000)** — the OS / Mission Control UI.
4. (Optional) **Mirofish (5001)** and **TimesFM (5002)** if you want simulation / forecasting.

---

## 1. The three projects, one-line each

### DecisionGraph
**The Operating System for Institutional Memory.** FastAPI app + Python library. Ingest
PDFs / URLs / GitHub repos / decisions into a dual graph (semantic + structural),
queryable via REST or via MCP from any agent.

* Code: `decisiongraph/`, `server.py`
* Read more: `ARCHITECTURE.md`, `DOCUMENTATION.md`

### AgentNet
**Sandboxed agent runtime** that lives inside the DG package. Scoped, time-limited
grants. Signed audit ledger. Agent reputation. Inter-agent message bus. The
substrate the OS uses so multiple agents can write to a shared workspace safely.

* Code: `agentnet/`, `decisiongraph/agent_*.py`

### DG Mission Control
**Agentic OS that turns one prompt into a working full-stack app.** A dispatcher LLM
reads a 1,200-line master prompt, decomposes a task per feature, picks from 63 agent
personas + 250 skill modules + 115 rule sets, builds the site, self-corrects compile
errors, and ingests the result back into DG as long-term memory.

* Code: `os_build.py`, `dashboard_server.py`, `dashboard/`, `ecc/` (catalog + prompts),
  and the OS modules inside `decisiongraph/` (`kernel.py`, `dispatcher.py`,
  `scaffold.py`, `llm_executor.py`, `run_state.py`, `catalog.py`, `dg_sync.py`,
  `dg_local_ingest.py`, `codebase_local.py`)

---

## 2. Setup — once per machine

### Prerequisites
* **Python 3.11+**.
* **Node 18+** (only if you plan to run DG Mission Control — built sites are Next.js).
* **Git**.
* An **LLM gateway** speaking the Anthropic Messages API on `http://localhost:8080`.
  Easiest path: `npm i -g antigravity-claude-proxy && acc start` (uses Google Gemini
  via your Google account). Or point at any compatible proxy you already run.
* Optional: **Docker Desktop** if you want to use Postgres-backed apps built by the OS.

### Clone + Python deps
```bash
git clone https://github.com/Aloneinthemultiverse/Decision_Graph.git
cd Decision_Graph
pip install -r requirements.txt
cp .env.example .env       # then edit with your LLM creds
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

## 3. Running each service — exact commands

### 3.1 LLM proxy on port 8080
```bash
# antigravity-claude-proxy (recommended — Google Gemini backend)
npm i -g antigravity-claude-proxy
acc accounts add        # link a Google account (interactive OAuth)
acc start               # daemon on 0.0.0.0:8080
acc status              # confirm it says "Proxy is active"
```
Smoke test:
```bash
curl -X POST http://127.0.0.1:8080/v1/messages \
  -H "content-type: application/json" \
  -d '{"model":"gemini-2.5-flash","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
# expect HTTP 200
```

### 3.2 DecisionGraph on port 8000
```bash
# Windows
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
python server.py

# macOS / Linux
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python server.py
```
Open **http://localhost:8000** — landing page.
Open **http://localhost:8000/app** — the Stitch UI (graph viewer, query mode, etc.).
API docs at **http://localhost:8000/swagger**.

> The `PYTHONIOENCODING=utf-8` is mandatory on Windows. The background job
> worker prints a Unicode arrow during recovery; without UTF-8 encoding the
> worker thread dies at boot and every queued job hangs forever.

### 3.3 DG Mission Control dashboard on port 7000
```bash
python dashboard_server.py
```
Open **http://localhost:7000**. This is the live build dashboard with the Dispatch
button. Requires the **proxy (8080)** to be up. DG (8000) is optional — without it
you still get the build pipeline, just no DG ingest at the end.

### 3.4 Mirofish (simulation studio) on port 5001
Only needed if you want to use DG's Simulation Studio feature.
```bash
python mirofish_lite.py
```
DG's UI (`/app`) auto-detects Mirofish on 5001.

### 3.5 TimesFM (forecasting) on port 5002
Only needed if you want to use DG's `/api/forecast` endpoint.
```bash
python timesfm_service.py
```
Listens on **127.0.0.1:5002** (loopback only — not exposed externally).

### Quick "everything up" check
```bash
curl -s -o /dev/null -w "8080 %{http_code}\n" http://127.0.0.1:8080/v1/messages
curl -s -o /dev/null -w "8000 %{http_code}\n" http://127.0.0.1:8000/api/status
curl -s -o /dev/null -w "7000 %{http_code}\n" http://127.0.0.1:7000/
curl -s -o /dev/null -w "5001 %{http_code}\n" http://127.0.0.1:5001/health
curl -s -o /dev/null -w "5002 %{http_code}\n" http://127.0.0.1:5002/health
```

---

## 4. Using DecisionGraph

### 4.1 Web UI
**http://localhost:8000/app** — ingest documents, view the graph, query, deep-research.

### 4.2 Python library
```python
from decisiongraph import DecisionGraph

dg = DecisionGraph(storage_dir="./storage/my_workspace")
dg.ingest("docs/architecture.pdf")
print(dg.query("What did we decide about authentication?"))
```

### 4.3 As an MCP server (Claude Code / Cursor / Continue / any MCP client)
Claude Code can **query and ask questions about the DB** directly. Run the MCP
server as a stdio subprocess:
```bash
python -m decisiongraph.mcp_server
```
Add this to your Claude Code MCP config (`~/.claude.json` or your IDE's MCP config):
```json
{
  "mcpServers": {
    "decisiongraph": {
      "command": "python",
      "args": ["-m", "decisiongraph.mcp_server"],
      "cwd": "/absolute/path/to/Decision_Graph",
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "DG_CODE_GRAPH_DB": "/absolute/path/to/storage/code_graph.db"
      }
    }
  }
}
```
Now Claude Code has the DG tools. It can call them mid-conversation:
* `recall("auth decisions")` — keyword recall of past decisions
* `query("how does the booking flow work?")` — deep RAG query
* `find_callers("validate_token")` — who calls this function across the codebase
* `blast_radius("validate_token")` — what breaks if I change it
* `topology(top_god=5)` — structural hotspots in the indexed code
* `update_files([...])` — incrementally update the code graph after an edit
* `track_decision(...)`, `track_code_edit(...)`, `recent_code_edits(...)`
* `ingest_github(url)` — pull a public repo into DG memory

(See `decisiongraph/mcp_tools.py` for the full set of 30+ tools.)

Setup walkthrough also in `SETUP_CLAUDE_CODE.md`.

---

## 5. Using DG Mission Control

After the proxy (8080) and dashboard (7000) are up, just open
**http://localhost:7000**, type your task into the Launch screen, and click
**Dispatch ▶**.

Or from the CLI:
```bash
DISPATCH_MODEL=gemini-2.5-flash BUILD_MODEL=gemini-2.5-flash \
  python os_build.py "Build a website for a tea house named Steep — hero, menu, gallery, reservation form."
```
What happens:
1. **Dispatcher** reads `ecc/DISPATCHER_PROMPT.md`, decomposes the task per feature,
   picks personas/skills/rules per subtask.
2. **Builder** executes each subtask AS the chosen persona, applying its skills.
3. **Boot gate** runs `npm install`, auto-installs missing dependencies, runs
   `tsc --noEmit`, and dispatches `build-error-resolver` until the project compiles.
4. **DG ingest** pushes the finished project's blueprint + code graph into DG as
   long-term memory.
5. **Dashboard** streams the whole thing live — DAG, agent logs, graph fill.

Built sites land in `.tmp/os_site/`. To run one:
```bash
cd .tmp/os_site
npm run dev    # serves on http://localhost:3000
```

---

## 6. Storage layout (so you know what's where)

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

## 7. Honest known issues

* **Windows `cp1252` console** — set `PYTHONIOENCODING=utf-8` before `python server.py`,
  otherwise the job worker thread dies on a Unicode character at boot.
* **`localhost` vs `127.0.0.1`** — on Windows, Python's `httpx` resolves `localhost` to
  IPv6 first. Use `127.0.0.1` in `LLM_BASE_URL`.
* **Per-workspace cache** — the FastAPI server caches each workspace's DG instance in
  memory. If you write to the workspace pickle from outside the server, the running
  server keeps serving the stale in-memory copy until restart. Dashboard's Graph
  Viewer bypasses this by reading `graph_clean.pkl` directly off disk.
* **`entity_resolution()` AxisError on tiny graphs** — guard for `len(nodes) < 2` if
  you hit it.

---

## 8. License

See `LICENSE`.
