# DecisionGraph — Complete Documentation (A → Z)

> **What it is, in one line:** DecisionGraph is an *Institutional Memory Operating System* — it turns the scattered decisions, conversations, and knowledge of an organization into a single living, queryable, self-improving knowledge graph that humans *and* AI agents can reason over.

This document explains every feature from A to Z. Part 1 is the full feature catalogue (the map). Part 2 explains each feature in depth, "documentary" style — what it is, why it exists, how it works, and how it connects to the rest of the system.

---

# PART 1 — THE FEATURE MAP (A to Z)

| # | Feature | One-line purpose |
|---|---------|------------------|
| A | **Knowledge Graph Core (GraphRAG)** | A NetworkX graph of entities, decisions, and sessions — the substrate everything sits on. |
| B | **Multi-Tenant Workspace Isolation** | Every visitor/company gets a private, isolated memory — no data bleed. |
| C | **Decision Memory & Evolution** | Decisions are first-class objects with confidence, outcomes, supersession, and relationships. |
| D | **Compiled Truth** | The system distils many raw decisions on a topic into one current "this is what we believe now." |
| E | **Immutable Timeline** | An append-only audit history of how the organization's thinking evolved. |
| F | **The Dream Cycle** | A background consolidation pass — decay, dedupe, relink, pattern-mine, recompile. |
| G | **Query Engine + Intent Routing (AUTO mode)** | Classifies a question and auto-selects the right retrieval strategy. |
| H | **Hybrid Retrieval (keyword + vector + RRF fusion)** | Combines lexical and semantic search for better recall. |
| I | **Multi-Graph Beam Query** | Reasoning that traverses across multiple graphs/communities and time. |
| J | **Recall** | Direct entity-centric memory lookup ("what do we know about X?"). |
| K | **Knowledge Ingestion (text / documents)** | Pull raw text and documents into the graph. |
| L | **Web / URL Ingestion** | Build knowledge directly from a web page. |
| M | **YouTube / Media Ingestion** | Build knowledge from video transcripts (gated, see notes). |
| N | **Durable Job Queue** | SQLite-backed async jobs with restart-replay recovery. |
| O | **Sessions (conversational memory)** | Multi-turn discussion threads that become graph memory. |
| P | **Simulation Engine** | Run scenario simulations against the institutional memory. |
| Q | **Cross-Company Anonymized Patterns / Wisdom** | Shared, anonymized lessons across tenants. |
| R | **MCP Server (7 tools)** | Any AI assistant/agent can plug into the memory via Model Context Protocol. |
| S | **Integrations Layer** | Connect/sync with external systems and broadcast decisions. |
| T | **Evals Capture & Replay** | Redacted query capture and LLM-free retrieval replay for evaluation. |
| U | **Server-Managed LLM Gateway** | A single configurable LLM endpoint, admin-gated. |
| V | **Local Model Path (Ollama / MiroFish Lite)** | Decision extraction on a local model — keeps data in-boundary. |
| W | **Animated Landing + Single-Page App UI** | Polished front end: landing page + 8-tab SPA incl. the "Brain" tab. |
| X | **Security & Governance Posture** | Admin-token gating, secrets hygiene, isolation, audit trail. |
| Y | **Public Deployment (stable tunnel)** | Stable public URL surviving restarts for live demos. |
| Z | **Test Suite (110 passing)** | Automated proof of isolation, evolution, integrations, modes. |

---

# PART 2 — EACH FEATURE, IN DEPTH

## A — Knowledge Graph Core (GraphRAG)

**What it is.** The heart of the system. Everything ingested — decisions, conversations, documents, web content — is decomposed into a graph: nodes are entities/concepts/decisions, edges are relationships between them. It's built on NetworkX.

**Why it exists.** Plain vector search ("embed everything, find nearest") loses *structure* — it can't follow "this decision caused that incident, which changed this policy." A graph keeps the relationships, so the system can *reason over chains*, not just match text.

**How it works.**
- Content is parsed into entities and relationships and added to the graph.
- **Louvain community detection** groups densely related nodes into communities (think: topics/clusters that naturally emerge).
- Retrieval uses **beam search** over the graph — it expands the most promising paths instead of brute-forcing, so multi-hop reasoning ("why did X happen?") stays tractable.
- This is the "GraphRAG" approach: retrieval-augmented generation where the retrieval is graph-structured, not flat.

**Connects to:** Compiled Truth (D) compiles per community; the Query Engine (G/H/I) traverses this graph; the Dream Cycle (F) reshapes it.

---

## B — Multi-Tenant Workspace Isolation

**What it is.** Every visitor/company gets their own private workspace — separate storage directory, separate graph, separate decision memory. Identity is by cookie (`dg_ws`); no login required for the demo.

**Why it exists.** This is the difference between a toy and a real product. If two companies share one graph, one company can see the other's decisions — fatal for an institutional-memory product. It also lets hackathon judges test independently without colliding.

**How it works.**
- A `WorkspaceManager` creates/loads per-token workspaces, evicts idle ones (LRU), and shares a single heavy embedding model across all of them (memory efficiency without sharing *data*).
- Each `Workspace` lazily builds its own `DecisionGraph`, `EnterpriseHub`, and `DecisionMemory`, all pointed at that workspace's own storage directory.
- A request-binding middleware resolves the current workspace from the cookie and routes the request to the right isolated objects.

**Hardening history.** A real isolation breach was caught by tests (decision memory was defaulting to a global pickle) and fixed so each workspace loads strictly from its own directory. This is the spine that the future "external agent access" product is built on.

**Connects to:** Everything — it's the boundary all other features run inside. It's also the foundation of the planned per-agent scoped access (see `PLAN.md`).

---

## C — Decision Memory & Evolution

**What it is.** A decision isn't just a stored sentence. It's an object with: a confidence score, a timestamp, an outcome (was it right?), relationships to other decisions, and the ability to be *superseded* by a newer decision.

**Why it exists.** Organizations don't make a decision once and freeze it — decisions age, get proven wrong, get replaced. Memory that can't represent *evolution* is just a logbook. This makes the memory honest about uncertainty.

**How it works.**
- **Confidence decay:** older, unreinforced decisions lose confidence over time (so stale beliefs don't masquerade as current truth).
- **Outcomes:** you can record what actually happened after a decision; this feeds back into trust.
- **Supersede:** decision A can be explicitly replaced by decision B — the chain is preserved, not deleted.
- **Relationships / autolink:** decisions in the same community get zero-LLM `shares_community` links automatically, so related decisions are connected without expensive model calls.
- **Chains:** you can pull the causal/decision chain for any decision.

**Connects to:** Compiled Truth (D) reads the current high-confidence set; the Timeline (E) records every change; the Dream Cycle (F) runs decay and relinking.

---

## D — Compiled Truth

**What it is.** For any topic/community, the system compiles the many raw decisions into a single, current, synthesized "this is what we currently believe" statement.

**Why it exists.** When someone asks "what's our position on X?", dumping 40 historical notes is useless. People want the *current answer*, with the history available if they want it. Compiled Truth is the organization's working memory.

**How it works.** Per community, related decisions (weighted by confidence/recency) are compiled into a `compiled` entry stored alongside the graph. It can be recomputed on demand (`recompile`) or refreshed automatically by the Dream Cycle.

**Connects to:** Surfaced in the UI "Brain" tab; recompiled by the Dream Cycle (F); built on Decision Memory (C).

---

## E — Immutable Timeline

**What it is.** An append-only history: every decision, change, supersession, and outcome is recorded in order and is **not rewritten**.

**Why it exists.** Two reasons. (1) You can see *how* thinking evolved, not just where it landed — invaluable for postmortems and "why did we ever decide that?". (2) It's an **audit trail by design** — which is exactly what makes the future "external agents touch company data" model trustworthy (you can prove who touched what).

**How it works.** Changes are journaled rather than mutated in place; the timeline is queryable per topic and across the workspace.

**Connects to:** Decision evolution (C); the planned agent-access governance (every agent action gets a Timeline entry).

---

## F — The Dream Cycle

**What it is.** The signature feature. A background "consolidation" pass — analogous to sleep consolidation in a brain. While the system is idle, it improves itself.

**Why it exists.** Most systems are static between user actions. An institutional memory should get *smarter on its own*. This is the feature judges remember.

**How it works (the pass).**
1. **Decay** stale confidence on old, unreinforced decisions.
2. **Dedupe** overlapping/duplicate decisions.
3. **Relink** related decisions (rebuild community edges).
4. **Pattern-mine** cross-company anonymized patterns.
5. **Recompile** Compiled Truth.
6. **Save** the consolidated state durably.

It runs as a durable job (see N) so a server restart can't lose it midway.

**Connects to:** Decision Memory (C), Compiled Truth (D), Patterns/Wisdom (Q), Job Queue (N).

---

## G — Query Engine + Intent Routing (AUTO mode)

**What it is.** When a question comes in, a classifier reads it and routes it to the right retrieval strategy automatically. The user just asks naturally.

**Why it exists.** Different questions need different reasoning. "Who owns billing?" is a single-hop entity lookup. "Why did the outage happen and how did our policy change after?" needs multi-hop causal traversal across time. One fixed strategy is wrong for half the questions.

**How it works.**
- `classify_intent` buckets the question as **temporal**, **entity**, **event**, or **general** using linguistic cues ("when/history/trend" → temporal; "who/which team/owns" → entity; "what happened/why did/postmortem" → event).
- `intent_to_mode` maps intent → mode:
  - temporal → **deep** (multi-step traversal across time)
  - event → **deep** (causal/decision chains)
  - entity → **session** (graph-centric single pass)
  - general → **session**
- The UI shows an **intent badge** so you can *see* it routing live.

**The three modes in plain terms.**
- **Fast** — quick keyword + vector lookup, single pass. Best for simple facts.
- **Session (graph)** — single-pass graph traversal, entity/relationship aware. Best for "who/which team."
- **Deep** — multi-step beam traversal across graph and time, follows causal chains. Best for "why/how did it evolve/postmortem."

**Connects to:** Hybrid Retrieval (H), Multi-Graph Beam (I).

---

## H — Hybrid Retrieval (keyword + vector + RRF fusion)

**What it is.** Retrieval that runs both a keyword (lexical) search and a semantic (vector) search, then fuses the two ranked lists with **Reciprocal Rank Fusion (RRF)**.

**Why it exists.** Vector search misses exact terms (codenames, IDs, acronyms). Keyword search misses paraphrases. Fusing both recovers what either alone would drop. Optional via a checkbox in the UI.

**How it works.** `keyword_search` + embedding search each produce a ranking; `rrf_fuse` combines them so items ranked highly by *either* method rise. `check_uncertainty` flags low-confidence answers.

**Connects to:** Query Engine (G).

---

## I — Multi-Graph Beam Query

**What it is.** Beam-search reasoning that can traverse across multiple graphs/communities (and time), not just one local neighborhood.

**Why it exists.** Deep questions cross topic boundaries ("how did the security incident change the hiring policy?"). Single-community retrieval can't answer that. Beam search keeps the best partial paths and expands them, making multi-hop tractable.

**Connects to:** Query Engine deep mode (G), Knowledge Graph Core (A).

---

## J — Recall

**What it is.** A direct, entity-centric memory probe: "what do we know about <entity>?" — returns the relevant decision/knowledge cluster for that entity rather than running a full Q&A.

**Why it exists.** Sometimes you don't have a question, you have a *subject*. Recall is the "pull the file on X" operation. (The judge who tested live used exactly this — `recall?entity=hello`.)

**Connects to:** Knowledge Graph Core (A), surfaced in the Brain tab.

---

## K — Knowledge Ingestion (text / documents)

**What it is.** The pipeline that takes raw text and documents and turns them into graph memory (entities, relationships, decisions).

**Why it exists.** Memory is only as good as what goes in. There are personal and per-company ingestion paths so content lands in the right workspace/company scope.

**How it works.** Document handlers parse content; the ingest pipeline extracts structure (using the local model path, see V) and writes nodes/edges into the appropriate workspace graph.

**Connects to:** Web (L) and Media (M) ingestion reuse this pipeline; Job Queue (N) runs it async.

---

## L — Web / URL Ingestion

**What it is.** Give it a URL; it fetches and cleans the page text (BeautifulSoup) and builds knowledge from it.

**Why it exists.** "The AI can directly get info from a website and make knowledge from it" — turn the open web into institutional memory without copy-paste.

**How it works.** `fetch_url_text` extracts readable content; thin/empty pages are handled gracefully (clean 422 rather than a 500). Runs as an async job to dodge tunnel timeouts (see N).

**Connects to:** Ingestion pipeline (K), Job Queue (N).

---

## M — YouTube / Media Ingestion

**What it is.** Build knowledge from a YouTube transcript or media file.

**Why it exists.** A lot of organizational knowledge is in talks/recordings.

**Important honest note.** The media-transcription path is **deliberately gated off**. By explicit project constraint, the Gemini API key is never set; media transcription that would require it is disabled (the endpoint returns 503 and the UI hides it). **URL and YouTube-transcript ingestion run through the existing local LLM gateway path only** — no external paid key is used. This was a hard requirement and is honored in code (`media_ingest_enabled` stays false).

**Connects to:** Ingestion pipeline (K), Job Queue (N), LLM gateway (U).

---

## N — Durable Job Queue

**What it is.** A SQLite-backed job queue. Long tasks (web/media ingest, the Dream Cycle) are enqueued and processed asynchronously; the API returns a `job_id` and the client polls.

**Why it exists.** Two real problems: (1) public tunnels enforce a ~100s request timeout — long ingests would die; (2) a server restart shouldn't lose in-flight work.

**How it works.** Jobs are persisted in SQLite. On startup, `_recover()` requeues jobs that were interrupted (restart-replay recovery). Workers resolve the right workspace by token and take that workspace's lock for their own writes.

**Connects to:** Web/Media ingest (L/M), Dream Cycle (F).

---

## O — Sessions (conversational memory)

**What it is.** Multi-turn discussion threads. You start a session, exchange messages, and end it — and the conversation is folded into graph memory.

**Why it exists.** A lot of decisions are made in conversation. Capturing the thread (not just a final note) preserves the reasoning, not only the conclusion.

**How it works.** `sessions/start` → `sessions/message` (turns) → `sessions/end`; the session content is extracted into the sessions-scoped graph and linked to decisions.

**Connects to:** Knowledge Graph Core (A), Discussion module.

---

## P — Simulation Engine

**What it is.** Run scenario simulations against the institutional memory — explore "what would we likely decide / what happened last time in a situation like this?"

**Why it exists.** Memory becomes far more valuable when it can be *projected forward*, not just queried backward. Simulations can be stored back as artifacts.

**How it works.** `simulation/run` executes a scenario; results are retrievable by `sim_id`; `simulation/{sim_id}/store` persists a result into memory.

**Connects to:** Query/reasoning stack (G–I), Decision Memory (C). Covered by `test_simulation.py`.

---

## Q — Cross-Company Anonymized Patterns / Wisdom

**What it is.** Lessons that recur across many tenants are mined and surfaced in an **anonymized** form ("organizations that did X often saw Y") — without exposing any one company's raw data.

**Why it exists.** Network effect: the system gets wiser as more orgs use it, while isolation (B) is preserved. This is a strategic moat, not just a feature.

**How it works.** The Dream Cycle's pattern-mining step (F) aggregates de-identified signals; exposed via company patterns/wisdom endpoints.

**Connects to:** Dream Cycle (F), Isolation (B) — the anonymization is what lets these coexist.

---

## R — MCP Server (7 tools)

**What it is.** A Model Context Protocol server exposing DecisionGraph to *any* MCP-capable AI assistant/agent. Also reachable over HTTP at `/api/mcp/*`.

**The 7 tools:** `list_companies`, `query_knowledge`, `get_past_decisions`, `store_decision`, `ingest_document`, `get_stats`, `recall`.

**Why it exists.** This is the feature that turns DecisionGraph from an app into a *platform*. Any agent can read/write the institutional memory through a standard protocol — the foundation of the planned agent marketplace (see `PLAN.md`).

**How it works.** The MCP server registers the tool schemas; an HTTP wrapper lets the UI (and external callers) list tools, check status, read config, and call a tool by name with a chosen company scope.

**Connects to:** Everything readable/writable — it's the programmatic front door. Directly underpins the future product direction.

---

## S — Integrations Layer

**What it is.** A pluggable layer to connect external systems: configure an integration, health-check it, sync data, and **broadcast** a decision or session out to connected systems.

**Why it exists.** Institutional memory shouldn't be a silo — decisions often need to flow to where work happens.

**How it works.** Per-named-integration config (get/set/delete), health, sync, and broadcast endpoints. Covered by `test_integrations.py`.

**Governance note.** Broadcasting/sharing externally is a sensitive action by design — the system treats outbound sharing as something that needs explicit, governed handling rather than silent automation.

---

## T — Evals Capture & Replay

**What it is.** Captures queries in **redacted** form and can replay retrieval **without the LLM**, to evaluate retrieval quality deterministically.

**Why it exists.** You can't improve what you can't measure, and you don't want eval to cost LLM calls or leak sensitive query text. LLM-free replay makes evaluation cheap, repeatable, and privacy-safe.

**Connects to:** Query Engine (G–I). Endpoints: `/api/evals`, `/api/evals/replay`.

---

## U — Server-Managed LLM Gateway

**What it is.** The system calls a single configurable LLM endpoint (the deployment's own gateway, e.g. a local proxy that presents a clean API). LLM settings are admin-token-gated.

**Why it exists.** Centralizing the LLM endpoint means: no per-user keys, controlled cost, and the ability to point the whole system at a local/owned model for data-boundary reasons. The `/api/connect` and settings paths were deliberately made inert/admin-gated so end users can't repoint the model.

**Operational note.** This gateway is an external process to the app. If it is down, the app still loads and graph/Brain/UI features work, but LLM-backed answers won't generate until it is running.

**Connects to:** Query Engine (G), Ingestion (K–M).

---

## V — Local Model Path (Ollama / MiroFish Lite)

**What it is.** Decision extraction runs on a **local** model via **MiroFish Lite** (a faithful drop-in of MiroFish's multi-step async extraction API) on local Ollama (`qwen2.5:3b-instruct`).

**Why it exists.** Keeping extraction on a local model means raw company content doesn't have to leave the trust boundary to be processed. This is also the technical foundation of the planned sandbox/egress-locked agent model.

**Connects to:** Ingestion (K), Sessions (O), and the future security architecture in `PLAN.md`.

---

## W — Animated Landing + Single-Page App UI

**What it is.** A polished front end: an animated landing page (`landing.html`) and an 8-tab single-page app (`index.html`) including the **Brain** tab.

**The Brain tab** surfaces the differentiators in one place: Compiled Truth, immutable Timeline, Recall, the Dream Cycle, and Export.

**Other UI features.** Query AUTO mode with a live intent badge and an optional hybrid-retrieval toggle; a "build knowledge from web/media" panel with async job polling; an MCP company selector; a "your private workspace" badge. The landing page has scroll-reveal animations, an inline animated SVG knowledge-graph illustration (edges draw in, nodes pop, a scan sweep), and reduced-motion support for accessibility.

**Connects to:** It's the human surface over A–V.

---

## X — Security & Governance Posture

**What it is.** The current safety model: per-workspace isolation (B), admin-token-gated settings/LLM config (U), secrets hygiene (`.gitignore` excludes `.env*`, credentials, tokens, storage, pickles, DBs, logs), and an immutable audit Timeline (E). Outbound sharing/broadcast is treated as a sensitive, governed action (S).

**Why it exists.** An institutional-memory product lives or dies on trust. This posture is also the launch point for the planned hardening (per-agent scoped access + sandbox + egress lock) described in `PLAN.md`.

**Honest scope note.** Today's posture is solid for a multi-tenant demo. The *productized* security layer (sandboxed external agents, per-agent scoped tokens, default-deny egress with audited allowlist, at-rest encryption) is explicitly future work, phased in `PLAN.md`.

---

## Y — Public Deployment (stable tunnel)

**What it is.** The app is exposed publicly via a **stable reserved tunnel domain** so the URL survives restarts — important for live judging/demo.

**Why it exists.** Ephemeral tunnels rotate their URL and time out long requests; a serverless host can't run a stateful, local-model app. A stable reserved tunnel was the pragmatic fit for a self-hosted stack that must stay reachable at one address.

**Runtime components (the stack):** DecisionGraph app, MiroFish Lite, Ollama (local model), the tunnel, and the external LLM gateway. A single check confirms all are up before a demo.

---

## Z — Test Suite (110 passing)

**What it is.** Automated tests proving the system actually works: isolation, decision evolution, gbrain-feature parity, MCP, multi-graph reasoning, integrations, simulation, modes.

**Why it matters.** Most prototypes have zero tests. 110 passing tests is hard evidence of engineering rigor — it's what lets you claim "this works" rather than "this demoed once." The isolation tests in particular are now the safety foundation for the product roadmap.

**Files:** `test_isolation.py`, `test_evolution.py`, `test_gbrain.py`, `test_mcp.py`, `test_multi_graph.py`, `test_integrations.py`, `test_simulation.py`, `test_modes.py`, `test_v2.py`.

---

# CLOSING SUMMARY

DecisionGraph is not "a chatbot over documents." It is a layered system:

1. **A graph substrate** (A) inside **strict tenant isolation** (B).
2. **Decisions as evolving objects** (C) that get distilled into **Compiled Truth** (D) and recorded on an **immutable Timeline** (E).
3. A memory that **improves itself while idle** via the **Dream Cycle** (F).
4. A **reasoning layer** that **routes intent automatically** (G) and retrieves with **hybrid + multi-graph beam** methods (H–J).
5. Many ways to **feed it** — text, documents, web, media, sessions, simulation (K–P) — through a **durable async queue** (N).
6. A **network surface** — **MCP** (R) and **integrations** (S) — that turns it from an app into a platform.
7. Backed by **local-model processing** (V), an **admin-gated LLM gateway** (U), **evaluation tooling** (T), a **polished UI** (W), a **defensible security posture** (X), **stable public deployment** (Y), and **110 passing tests** (Z).

The one sentence that captures the product: **an organization's memory lives in DecisionGraph, gets smarter on its own, and any human or AI agent reasons over it through one governed, audited layer.**

*See `PLAN.md` for the forward roadmap (sandboxed external-agent access, scoped per-agent permissions, the agent marketplace).*
