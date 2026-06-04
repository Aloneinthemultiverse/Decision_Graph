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

## 2. The three projects — full breakdown

> 📖 **Full visual guide:** see [`docs/VISUAL_GUIDE.md`](docs/VISUAL_GUIDE.md) —
> a page-by-page walkthrough in plain English of every screen in the app
> (Knowledge Base, Query Terminal, Brain, MCP · AgentNet, Simulation Studio,
> Marketplace, Agents, Dashboard, Orchestration, and the how-to recipes).
> The same guide is served live at **http://localhost:8000/docs** once you
> start the DG server.

### 2.1 DecisionGraph

#### What it is, in one sentence
**A long-term memory you can plug into any AI agent or team.** You feed it
documents, code, websites, or just notes about decisions you made — and it
remembers them in a way an AI can search, reason about, and build on later.

#### Why it exists — the problem it solves
AI agents have a memory problem. Every new chat starts from zero. They've never
seen your codebase, never read your docs, don't know what your team decided
last quarter. So they guess — and guess wrong.

Vector databases help, but only a little. A vector search returns *"things
that sound similar to your question"* — not *"things that are actually
related to the question you asked."* It can't tell you who calls a function,
what breaks if you change one, or what you decided about authentication six
months ago.

DecisionGraph solves this by **building two graphs side by side**:
* A **knowledge graph** — concepts and how they connect (what an LLM "reads
  about" your data).
* A **code graph** — the actual structure of your code (who calls what, what
  imports what, what breaks if you touch it).

Then it stores every decision your team or AI makes as a **first-class
memory node** with the reasoning, confidence, and outcome attached. So six
months later you can ask *"why did we pick JWT over sessions?"* and get the
real answer, not a hallucination.

#### What it does — features in plain language

**For your documents and notes**
* **Drop in anything** — PDFs, Markdown files, Word docs, websites, YouTube
  videos, audio/video files, even an entire GitHub repository. DG turns each
  one into a graph of concepts.
* **Resume on crash** — ingesting a big repo? If your laptop crashes
  midway, just run it again. It picks up where it left off.
* **Multiple users at once** — every visitor gets their own private
  workspace. Your data never mixes with anyone else's.

**For your code**
* **Who calls this function?** — get an answer in milliseconds, across
  the entire repo.
* **What breaks if I change this?** — DG walks the call graph backwards and
  tells you every file that depends on the one you're about to edit.
* **Find the chain from A to B** — show me how `signup_handler` reaches
  `send_welcome_email`, step by step.
* **Spot dead code, god-nodes, weird connections** — DG flags the
  500-call chokepoint files, the orphan modules nobody uses, the
  cross-folder edges that look suspicious.
* **Track every AI edit** — when an agent changes a file, DG logs *who*
  changed it, *when*, *where* in the file, and *why*. Permanent audit trail.

**For your decisions**
* **Save the decision and the reasoning.** Not just *"we chose JWT"* but
  *"we chose JWT because our microservices need stateless auth — discussed
  with the eng team on May 12, trade-off was the revoke story."*
* **Find similar past decisions before making a new one.** DG searches
  your decision history before you re-invent something you already
  solved.
* **Mark outcomes after the fact.** Did the decision work? Did it
  backfire? DG learns from both.
* **Age out stale decisions** so old guesses don't pollute new
  reasoning.

**For AI agents (Claude Code, Cursor, Antigravity)**
* **30 ready-made tools** the agent can call directly during a
  conversation. Want the AI to ask DG *"who calls this function?"*
  mid-task? It just does — no glue code, no integration work. See §7
  for the full list.
* **Onboarding brief in one call.** A new AI session starts already
  knowing what's in the repo, what was decided recently, who's been
  editing what.

**For teams**
* **Multi-tenant company memory.** Each company gets its own knowledge
  hub — shared across the team but isolated from other companies.
* **Federated queries across repos.** *"Show me every place
  `validate_token` is called across all 5 of our repos"* — one call.
* **PR risk scoring.** Hand DG a list of changed files; get back a
  0–100 risk score, a list of files that could break, and a
  recommended merge order.
* **Inline "# WHY:" comments become memory.** Drop a `# WHY:` or
  `# HACK:` or `# SAFETY:` comment in your code — DG captures it as
  a first-class node linked to the symbol. The tribal knowledge that
  usually dies in a code review now lives in the graph forever.

**For long-running work**
* **Background jobs survive crashes.** Big ingests run in a durable
  queue; if the server restarts, jobs resume.
* **Periodic "dream cycle"** that re-organizes the graph in the
  background — clustering related concepts, summarizing them,
  promoting frequently-used decisions into reusable "wisdom" patterns.

**For your existing tools**
* **Integrations** with Slack, Jira, Notion, Confluence, SharePoint,
  Google Workspace, and Supabase — pull decisions in, push wisdom
  out, broadcast events.
* **Simulation & forecasting** — DG hosts a Simulation Studio
  (via Mirofish on port 5001) and time-series forecasting (via
  TimesFM on port 5002).

**For ops**
* **Backup & restore** — one command to export your workspace,
  one to import it.
* **Rate limiting & circuit breakers** — protects against runaway
  LLM cost or accidental DoS from a misbehaving agent.

---

#### Technical depth (for engineers)

If you want to understand *how* it actually works under the hood:

* **Dual graph backend.** The semantic graph is a `networkx.MultiDiGraph`
  built from LLM-extracted (subject, relation, object) triples; entity
  resolution merges synonyms via cosine similarity on
  sentence-transformer embeddings (`all-MiniLM-L6-v2`); communities are
  detected with Louvain (`python-louvain`), then each cluster is named by
  the LLM in one shot. The structural graph is a SQLite DB populated by
  tree-sitter ASTs with proper import-scope resolution, storing
  `symbols`, `calls`, `imports`, `inherits`, and `rationales`.

* **Multi-tenant isolation.** `WorkspaceManager` lazily mints per-visitor
  workspaces at `storage/workspaces/<token>/`. Each carries its own
  `DecisionGraph` + `EnterpriseHub` + `DiscussionManager` and a
  `threading.RLock`. The embedding model is loaded once process-wide
  (`get_shared_embed_model()`) and shared read-only — without this,
  N visitors = N model copies = OOM at the first dozen users.

* **Ingest pipeline.** `core.DecisionGraph.ingest(path)` →
  `ingest.chunk_text` → parallel `extract_triples_safe` via
  `ThreadPoolExecutor(max_workers=10)` → `build_graph` →
  `merge_graphs(self.G, G_new)` → `entity_resolution` → `detect_communities`
  → `summarize_communities`. Each batch writes
  `checkpoint_<filename>.pkl`; on restart, `extract_all_triples` resumes
  from `state['chunk_idx']`.

* **GitHub repo ingest** (`codebase.ingest_github_url_v2`).
  `git clone --depth 1` → `_build_repo_blueprint` (one structured Markdown
  doc covering mission, concepts, file roles, per-function summaries,
  recent commits, PRs) → `code_graph.build_from_repo` (tree-sitter AST
  → SQLite) → `ws_dg.ingest(blueprint_path)` (feeds it through the same
  pipeline as a PDF). Incremental hash cache means re-ingesting the same
  repo only re-processes changed files.

* **Decision provenance schema.** Each decision is a graph node with:
  `id`, `question`, `answer`, `reasoning_summary`, `communities_used`,
  `confidence`, `outcome` (`unknown` / `succeeded` / `failed` / `partial`),
  `outcome_impact`, `access_count`, `timestamp`, plus signed audit
  metadata when written via AgentNet grants. `decay()` reduces
  confidence on stale entries; `supersede(old, new)` replaces an
  outdated decision and links them.

* **Dream cycle** (`consolidate.py`). Re-runs community detection over
  the merged graph, re-summarizes drifted clusters, computes a wisdom
  score from `confidence × outcome_impact × access_count`, and promotes
  high-scorers into `compiled` summaries that `query()` retrieves first.

* **MCP server** (`decisiongraph/mcp_server.py`). Native stdio
  JSON-RPC server registering 30 tools (see §7). Reuses the same
  workspace resolution as the FastAPI app via a thin wrapper.

* **REST surface** (`server.py`). FastAPI app on port 8000, ~70
  endpoints. Per-request workspace binding via a middleware that
  resolves a cookie (`dg_ws`) or query param (`?ws=`) to a `Workspace`
  object, then exposes it as `S.dg`/`S.hub`/`S.dm` for the rest of the
  request. Per-workspace sliding-window rate limiter on the
  `query`/`ingest`/`simulation` buckets.

* **Code graph internals** (`code_graph.py`). Schema: `files`,
  `symbols`, `calls`, `imports`, `inherits`, `rationales`, plus a
  `semantic_stale` flag table. Import-scope resolution does proper
  Python-import-style lookup so `find_callers("validate_token")` works
  even when the function is imported under an alias.
  `causal_radius()` joins call edges with related decisions/PRs from
  the knowledge graph to give a full impact picture.

* **Federated graph** (`code_graph_federated.py`). Takes a list of
  `workspace_roots`, opens each one's `code_graph.db` read-only,
  unions the results. Powers `federated_callers`,
  `federated_topology`, and `federated_rationale_search`.

* **Durable job queue** (`jobs.py`). SQLite-backed FIFO. A
  `threading.Thread` named `jobqueue` runs `_loop()`, claims `queued`
  rows, runs the registered handler (`ingest_github`, `dream`, …),
  marks `done`/`failed`. `_recover()` on boot re-queues anything
  stuck in `running` from a previous crash.

* **Stitch UI** (`stitch_ui/`). Plain HTML/CSS/JS (no React) served
  directly by FastAPI. Knowledge graph viewer uses `vis-network` for
  force-directed rendering. Decision Memory and Sessions are
  separate scopes on the same `/api/graph` endpoint.

**Code:** `decisiongraph/` package, `server.py`. Architecture deep-dive
in `ARCHITECTURE.md`; reference in `DOCUMENTATION.md`.

---

### 2.2 AgentNet

**Sandboxed agent runtime.** The safety layer that lets multiple agents act on
the same workspace without trampling each other. Lives inside the DG package
(`decisiongraph/agent_*.py`) with extended runtime + UI assets in `agentnet/`.

**Core capabilities**
* **Scoped, time-limited grants.** A grant is a signed token saying *"agent X
  may write to topics ['src/', 'tests/'], expires 2026-06-01T12:34Z."* Grants
  are minted per-task by the kernel, audited on every use, and auto-revoked at
  expiry.
* **Signed audit ledger.** Every grant mint, every tool call, every file edit,
  every decision logged — signed with the agent's key, replayable. You can
  reconstruct exactly what an agent did, when, and why.
* **Agent reputation.** Each agent carries a reputation score that decays on
  failures and rises on successful outcomes. A **circuit breaker** trips
  after `maxConsecutiveFailures`, blocking the agent until cooldown.
* **Inter-agent message bus.** Agents pass typed messages to each other
  (`post_message`, `read_messages`, `wait_for_message`) — supports
  request/response, broadcast, and async coordination.
* **Job marketplace.** `agent_job` exposes a registry of pending tasks
  agents can claim; reputation-weighted dispatch ensures the best-suited
  agent wins.
* **Agent sandbox.** Per-agent filesystem isolation (`agent_sandbox`) and a
  capability-gated tool palette — agents only see the tools their grant
  allows.
* **Agent catalog & crypto.** `agent_catalog` lists every registered agent
  with their capabilities; `agent_crypto` handles keypair generation,
  message signing, and grant validation.
* **Network registry.** `agent_network` is the directory service — discover
  agents by capability, query their reputation, route messages.
* **UI hooks.** `agentnet/ui/` contains preview UI components for agent
  inspection (see `agentnet/IDE_INTEGRATION.md`).

**Code:** `decisiongraph/agent.py`, `decisiongraph/agent_access.py`,
`decisiongraph/agent_catalog.py`, `decisiongraph/agent_crypto.py`,
`decisiongraph/agent_job.py`, `decisiongraph/agent_messages.py`,
`decisiongraph/agent_network.py`, `decisiongraph/agent_reputation.py`,
`decisiongraph/agent_sandbox.py`, plus the `agentnet/` folder.

---

### 2.3 DG Mission Control

**An Agentic OS that turns one prompt into a working full-stack app.** You type
*"Build a website for a tea house named Steep — hero, menu, gallery,
reservation form."* and an LLM dispatcher decomposes the task per feature,
picks the right agents and skills, builds the site, self-corrects compile
errors, and ingests the result into DG as long-term memory — all live on a
cinematic dashboard.

**Core capabilities**

*Dispatcher (LLM-driven, prompt-owned)*
* **1,200-line master prompt** (`ecc/DISPATCHER_PROMPT.md`) defines the
  decomposition protocol, dependency chain (PLAN → DATA → BACKEND → FRONTEND
  → SECURE → TEST → DEPLOY → DOCUMENT), section catalog (A–O), section-
  conflict resolution rules, language auto-detection, edge cases, and
  mandatory rules.
* **Domain-matched planner.** The dispatcher picks `architect` for software,
  `marketing-agent` for campaigns, `mle-reviewer` for data pipelines,
  `gan-planner` for product specs — not always `architect`.
* **Per-feature decomposition.** A hard rule in the prompt forces one
  subtask per named feature ("hero", "menu", "gallery", "reservation form")
  instead of stamping a generic "FRONTEND" step.
* **Multi-skill per agent.** Each subtask carries 3–6 relevant skills, not
  one — `frontend-patterns + react-patterns + design-system + accessibility`,
  not just `frontend-patterns`.
* **Stronger model for dispatch only.** `DISPATCH_MODEL` is configurable
  separately from `BUILD_MODEL` — run the decomposition on a smarter model
  (one call) while keeping the per-file builder cheap.

*ECC capability catalog*
* **537-entry catalog** (`ecc/TASK_CATALOG.md`, generated by `gen_catalog.py`):
  63 agent personas + 250 skill modules + 79 commands + 115 rules + hooks,
  contexts, schemas, integrations, plugins.
* **Persona body injection.** The builder doesn't just see a name; it gets
  the persona's full `agents/<name>.md` body as its system identity.
* **Skill body injection.** Each chosen skill's `SKILL.md` content is loaded
  into the prompt — real know-how, not one-line blurbs.
* **Rule glob matching.** Rules apply deterministically by file-path glob
  (e.g. `**/*.tsx → react/patterns`) — no LLM guessing.

*Builder (`decisiongraph/llm_executor.py`)*
* **Persona-driven.** Writes code AS the chosen persona, applying its
  skills, following its rules.
* **One file per call.** Each subtask file gets its own LLM call (no
  multi-file marker corruption).
* **Validation gate.** Rejects fragments, truncated files (unbalanced
  `{}/[]`, mid-statement endings, stray protocol markers) and re-prompts.
* **Retry-with-backoff + per-request timeout.** Survives proxy flaps;
  forces fresh connections per request to avoid keep-alive drops.

*Common CAG memory*
* **Cumulative blueprint.** After each subtask, the written files'
  signatures + exported shapes are appended to a shared `blueprint.md`
  that future subtasks see — so later steps don't re-invent the data
  layer the earlier step already wrote.
* **Cross-file shape contract.** The blueprint surfaces exported
  TypeScript types, function signatures, Prisma schema, data shapes —
  so a `Menu.tsx` consuming `menu.ts` sees the *actual* keys, not a
  guessed shape.

*Self-correcting boot gate*
* **`npm install` + auto-deps.** Scans every generated file's imports,
  diffs against `package.json`, adds anything missing (`zod`, `vitest`,
  `@testing-library/react`, etc.), reinstalls.
* **`tsc --noEmit` compile check.** Lists every file with errors.
* **`build-error-resolver` agent loop.** For each erroring file: full
  current contents + the exact error lines + the cross-file shape
  surface from CAG → the agent rewrites the file. Up to 3 rounds.
* **Monotonic guard.** If a round *increases* the error count vs. the
  previous round, the loop reverts to the previous best state and stops.
  Never keeps a regressive fix.

*OS-owned scaffold (`decisiongraph/scaffold.py`)*
* **Per-stack template registry.** Next.js boot files (`package.json`
  with pinned compatible versions, `tsconfig.json`, `next.config.js`,
  `tailwind.config.ts`, `postcss.config.js`, `jest.config.js`,
  `layout.tsx`, `globals.css`) emitted deterministically — never LLM-
  guessed.
* **Owned-file enforcement.** Scaffold OVERWRITES anything an agent
  wrote for these files (an agent inventing a `layout.tsx` with
  phantom imports = no longer breaks the build).
* **Recursive homepage assembly.** If the dispatcher emits components
  but no `page.tsx` that mounts them, scaffold synthesizes one by
  scanning `src/components/` (recursively).
* **Path normalization (`_norm`).** Coerces stray roots
  (`app/`, `pages/`, `components/`, `workspace/`) into the canonical
  `src/` layout so detection works regardless of what the dispatcher chose.

*DG integration (the OS becomes self-aware)*
* **Decision sync (`dg_sync.py`).** Every subtask's outcome is posted to
  DG via `track_decision` MCP — what was built, by which agent, with
  which skills, status.
* **Knowledge-graph ingest (`dg_local_ingest.py` + `codebase_local.py`).**
  After build, the OS runs the SAME blueprint pipeline DG uses for
  GitHub repos — but pointed at the local built site. Adds the
  project's nodes/edges/communities to the workspace's knowledge graph.
* **Persistent memory.** Multiple builds **accumulate** in one graph.
  Build #1's flower-delivery concepts, build #2's coffee-shop concepts,
  build #3's tea-house concepts coexist — you can ask DG *"show me
  everything I've ever built with Next.js + Prisma"*.

*Live cinematic dashboard (`dashboard/`)*
* **Mission Control screen.** Force-directed DAG of subtasks with
  curved SVG bezier connectors, glowing data packets flowing between
  nodes, box-by-box ignition animation as each agent starts, ripple
  rings around the active node, light-sweep on the running card,
  status icons (✔ done / ↻ running / ⧗ pending / ⚠ error).
* **Live agent terminals.** Each active agent gets a card with avatar,
  agent name, status pill, skill tags, files-written counter, live
  token counter, and a scrolling terminal showing `[AUTH]`/`[TRACE]`/
  `[IO]`/`[WARN]` log lines.
* **Particle network background** with cyan/violet aurora gradients
  that drift slowly.
* **Tile metrics** — task type, subtask progress (4/8), code graph
  nodes·edges, CAG file count, scaffold stack, boot-gate status,
  elapsed seconds.
* **Self-correction timeline.** Per-round error counts and revert
  events shown as a vertical log.
* **Graph Viewer screen.** Reads `graph_clean.pkl` and
  `decision_graph.pkl` **directly off disk** via a same-origin
  endpoint on the dashboard server — completely bypasses DG's
  workspace cache so you always see what's actually persisted.
* **Catalog browser** — searchable tabs for the 537 ECC entries
  (Agents, Skills, Commands, Rules, …).
* **Subtask Trace screen** — drill into any node to see its
  persona, skills, rules, and files written.
* **Use Cases launcher** — 6 domain cards (Build Software / Marketing /
  Product Spec / Data Pipeline / Research Report / Ops & Comms).
* **Launch screen** with a Dispatch button that POSTs to `/launch`
  on the dashboard server and spawns `os_build.py` in the background.

*Multi-domain capable*
* The dispatcher routes **non-code tasks** (marketing campaigns,
  research reports, PRDs) to domain-native pipelines (e.g.
  AUDIENCE → POSITIONING → CHANNEL-PLAN → ASSETS → CALENDAR for
  marketing) — scaffold and boot-gate auto-skip when the task isn't
  software.

**Proven builds:** Aperture photography studio, Bloom flower delivery,
Steep tea house, Slurp ramen, Crumb bakery, Hops brewery, Ember coffee
roastery, Margins bookstore, Paws pet adoption shelter, TaskFlow
Trello-clone (Prisma + Postgres) — each compiled to HTTP 200 with
30–77 indexed symbols and 100–330 call edges per build.

**Code:** `os_build.py`, `dashboard_server.py`, `dashboard/`, `ecc/`
(catalog + prompts), and OS modules inside `decisiongraph/`:
`kernel.py`, `kernel_cag.py`, `dispatcher.py`, `scaffold.py`,
`llm_executor.py`, `run_state.py`, `catalog.py`, `context_pack.py`,
`dg_sync.py`, `dg_local_ingest.py`, `codebase_local.py`.

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
