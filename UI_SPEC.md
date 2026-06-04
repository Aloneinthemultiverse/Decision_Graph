# DecisionGraph — UI Specification for Stitch

This document defines every screen, component, button, input, and data shape
for the DecisionGraph agent platform UI. Hand this to Stitch as-is; I'll wire
the resulting design back to the existing backend endpoints (already built and
tested, 164+ tests green).

---

## 0. Design tokens (give these to Stitch up front)

**Theme:** dark, premium, "Stitch" aesthetic. Same vibe as current landing.
```
Background          #0b0f17
Surface / panel     #121a26
Line / border       #1f2a37
Ink (text)          #e5e9f0
Muted text          #8b97a8
Accent (blue)       #3b82f6
Success             #22c55e
Danger              #ef4444
Warning             #f59e0b
Font                ui-sans-serif, Inter, Roboto
Mono                ui-monospace, Consolas
Border radius       9–14px
Subtle shadow       0 1px 0 rgba(255,255,255,0.03) inset
Animations          200–300ms ease-out; reduced-motion respected
```
Pills / status colors:
```
trusted / completed / granted / learning_written   #22c55e
established / grant_minted / job_started           #3b82f6
unproven                                           #94a3b8
flagged / revoked                                  #f59e0b
untrusted / failed / denied                        #ef4444
```

---

## 1. Sitemap

```
/                  Landing (already designed, keep)
/app               Main DecisionGraph SPA (already designed, keep)
/marketplace       Hire an agent  (NEW design)
/agents            Mint grant · run · live audit  (NEW design)
/dashboard         Company governance dashboard  (NEW design)
/orchestration     Multi-agent orchestration  (NEW design — coming feature)
```

Persistent top nav across the four NEW pages:
> **DecisionGraph**  ·  Marketplace · Agents · Dashboard · Orchestration · App ↗

Right side of nav: small chip *"your private workspace · {short cookie}"*.

---

## 2. /marketplace — Hire an Agent

### Purpose
A "LinkedIn for agents." Browse available agents, pick one, scope it to topics
in your knowledge graph, give it a task, hire it. The hire runs the agent
sandboxed against your graph and shows the result.

### Layout
- **Hero strip** (top):
  - H1: *"Agent Marketplace"*
  - Sub: *"Hire an agent — it runs sandboxed against your DecisionGraph, audited end to end."*
  - Right side: filter chips → `All` · `Read-only` · `Via MCP` · `Graph-aware`
- **Search bar:** placeholder `"search agents by skill, e.g. 'qa', 'summarize'"`
- **Agent grid:** responsive cards, min 320px, gap 16px.

### Agent card (one per catalog entry)
Fields shown:
- Avatar (initials in a colored circle from agent id)
- **Name** (large) + small chip with `agent_id`
- One-line `description`
- Tag row: small pills for each `tag` (e.g. `via-mcp`, `read-only`, `graph-aware`)
- **Inputs (expand-on-click):**
  - Topics (chips input, deny-by-default; comma-separated; pre-filled from `suggested_topics`)
  - Task (single line text; placeholder `"summarize the scoped knowledge"`)
- **Buttons:**
  - **Primary: `Hire`** (disabled if no topics) — calls `POST /api/marketplace/hire`
  - Secondary: `Clear result` — wipes local card output area

### Result panel (per card, slides down after Hire)
On success:
- ✓ Green status row: `job {job_id} · learning {learning_id} · {duration}s · token revoked`
- "Answer" block (large quote style):
  > "{job.result.answer}"
  >> citation: {job.result.citation}
- Small grey row: `via: mcp` or `via: staged` + `mcp_calls: N` (if MCP)
- Link: `View in audit timeline →` (jump to /dashboard?focus=job_id)

On error:
- ✗ Red status row: full server `detail` message
- "What this likely means" microcopy (one of):
  - *"Out of scope — give the agent a topic that exists in your graph."*
  - *"Unknown agent id."*
  - *"Docker sandbox not available. Check Docker is running."*

### States
- **Empty catalog:** centered card *"No agents in the catalog yet."*
- **Loading:** skeleton card grid (3 placeholders)
- **Running:** the card's Hire button becomes a spinner with text *"running sandboxed job…"*; rest of grid stays interactive

### Data contracts already implemented
```
GET  /api/marketplace/agents
     → { agents: [{ id, name, description, suggested_topics, tags, available }] }
POST /api/marketplace/hire
     body: { agent_id, allowed_topics: [str], task: str, ttl_seconds?: int }
     → { hire: {agent_id, agent_name, via}, job: { job_id, agent_name,
                 mcp_calls?, scope, learning_id, result:{answer,citation},
                 duration_s, token_revoked, via } }
```

---

## 3. /agents — Mint Grant · Run · Live Audit

### Purpose
The hands-on inspection page. Mint a scoped grant, run an agent manually,
watch the immutable audit log fill up in real time. This is the page that
makes the security model *visible*.

### Layout
Two columns on top (form cards), two full-width cards below.

#### 3.1 Card: "Mint a scoped grant"  *(top-left)*
H2 + caption: *"Deny-by-default · expiring · revocable"*
Inputs:
- **Agent name** (text, required) — placeholder `"billing-assistant"`
- **Allowed topics** (chips input, required) — placeholder `"pricing, refunds"`
- **TTL** (number, default 3600, unit `"seconds"`)
- Toggle: `Allow write` (default OFF) — small warning copy *"Writes are rare; most agents are read-only."*
Buttons:
- **Primary: `Mint grant`** — calls `POST /api/agent/grants`
On success: shows the token in a copy-to-clipboard pill + auto-fills the Run card.

#### 3.2 Card: "Run sample agent"  *(top-right)*
Inputs:
- **Agent token** (text) — auto-filled
- **Topics to request** (chips)
- **Task** (text)
Buttons:
- **Primary: `Run job`** — calls `POST /api/agent/run`
Output area (slide-down):
- ✓ `job {job_id} · learning {learning_id} · {duration}s · token revoked`
- Quoted answer block + citation line

#### 3.3 Card (full width): "Active grants"
Table columns: `Agent · Token (truncated, copy icon) · Topics (chips) · Write · State · Action`
- State: pill — `active` (green) / `revoked` (orange) / `expired` (grey)
- Action: button **`Revoke`** (only if active) → `POST /api/agent/grants/{token}/revoke`

#### 3.4 Card (full width): "Immutable audit timeline"
Caption: *"append-only — every grant, allow, deny, MCP call, job event recorded"*
Header right-side: `Refresh` button (manual) + auto-poll every 5s indicator
Table columns: `Time · Event (pill) · Agent · Detail`
- Pills colored per event (see token table above)
- Detail compact: `topic=…`, `tool=…`, `reason=…`, `learning_id=…`, `mcp_calls=…`

### Data contracts already implemented
```
POST /api/agent/grants
     body: { agent_name, allowed_topics:[str], ttl_seconds?:int, can_write?:bool }
     → { grant: { token, agent_name, allowed_topics, can_write, created_at,
                   expires_at, revoked } }
GET  /api/agent/grants                → { grants: [...] }
POST /api/agent/grants/{token}/revoke → { revoked: true, token }
GET  /api/agent/audit?limit=200       → { audit: [{ ts, event, agent_name, ... }] }
POST /api/agent/run                   → { job: ... }   (see marketplace shape)
```

---

## 4. /dashboard — Company Governance

### Purpose
The owner's view: KPIs across this company, cross-company anonymized network
directory, agents-in-this-company with reputation, searchable audit, and
emergency controls.

### Layout (top → bottom)

#### 4.1 KPI strip (4 tiles)
Each tile: big number + small label.
```
[ active grants ]   [ jobs completed (this co.) ]   [ scope denials (this co.) ]   [ companies on network ]
```

#### 4.2 Card: "Network directory" (anonymized)
Caption: *"opaque ids · aggregate counts only · no names, no tokens leave a boundary"*
Table: `Company (opaque id) · Agents · Jobs · Denials · Standings histogram`
- Standings shown as small inline pills: `trusted ×N · established ×M · flagged ×K`

#### 4.3 Card: "Agents in this company"
Header right: **`Revoke ALL active grants`** button (danger style, requires confirm modal)
Table: `Agent · Standing (pill) · Score (0–100 + bar) · Done · Failed · Denied · Learnings`
Sort: by score desc

#### 4.4 Card: "Audit search"
- Search input (with debounce) — placeholder `"filter by agent, topic, event, reason…"`
- Scrollable table same shape as the timeline on /agents

#### 4.5 (optional) Card: "Compliance posture" (purely informational)
Static rows with green checkmarks:
- ✓ Multi-tenant isolation (110 tests)
- ✓ Network-less sandbox (`--network none`)
- ✓ MCP scope gate audited per call
- ✓ At-rest encryption (when enabled)
- ✓ Immutable audit (append-only)

### Data contracts already implemented
```
GET  /api/agent/grants                  → { grants: [...] }
GET  /api/agent/reputation              → { reputation: [{ agent_name, score, standing, jobs_completed, jobs_failed, access_denied, learnings_written, ... }] }
GET  /api/network/companies             → { companies: [{ company, agents, jobs_completed, scope_denials, standings }], totals: {...} }
POST /api/agent/grants/revoke_all       → { revoked: int }
GET  /api/agent/audit/search?q=&limit=  → { audit: [...] }
```

---

## 5. /orchestration — Multi-Agent Orchestration  *(NEW — the next feature)*

### Purpose
Coordinate multiple agents working together on one company's graph. Two
modes the design must support, both feeding the SAME audit log:

1. **Pipeline mode** — Agent A → its learning is read by Agent B → and so on.
   Each stage gets its own scoped grant and runs in its own sandbox.
2. **Coordinator mode** — One "manager" agent hires sub-agents to do parts of
   a larger task. The manager gets a wider scope; sub-agents get narrower
   scopes; *all* go through the same MCP scope gate.

### Layout

#### 5.1 Top: "Compose orchestration"
Tabs at the top of this card:
- **`Pipeline`**  *(default)*
- **`Coordinator`**

##### Pipeline tab
A vertical chain builder:
```
┌──────────── Stage 1 ────────────┐
│ Agent: [select from catalog ▾]  │
│ Topics: [chips input]           │
│ Task: [text]                    │
│ [▼ pass output to next stage]   │
└─────────────────────────────────┘
        + Add stage
[Run pipeline]   [Save as template]
```
- Stages reorderable by drag.
- "pass output to next stage" toggle (default ON) — if on, stage N+1 gets
  Stage N's learning as part of its input context (the system already
  supports this via the round-trip; UI just exposes it).
- Final result shown below: each stage's answer + the final synthesized one.

##### Coordinator tab
```
Coordinator agent: [select an MCP-capable agent ▾]
Coordinator scope (topics): [chips — usually broader]
Goal: [textarea — the high-level task]

Sub-agents the coordinator may hire:
[ + add sub-agent  → name · catalog id · max scope chips ]

[Run]
```
- The coordinator agent receives a special MCP tool `hire_subagent(agent_id, topics, task)`
  (to be implemented). Each subhire creates its own scoped grant + sandbox + audit.

#### 5.2 Live "Run timeline"
A vertical timeline (newest at bottom) showing each stage / subhire:
```
● 03:42:01  Stage 1 · Knowledge QA (MCP) · scope=attention
            └─ result: "Transformer relies on attention mechanism"
● 03:42:04  Stage 2 · Summarizer · scope=attention
            └─ result: "The paper's thesis is the attention mechanism."
✓ Pipeline complete (2 stages · 3 MCP calls · 4.7s)
```

#### 5.3 "Run history"
A table of past orchestrations: `Time · Mode · Stages · Status · Duration · Final answer (preview)`.

### Buttons (full list)
- `Run pipeline` (primary)
- `Run coordinator` (primary)
- `+ Add stage` / `+ Add sub-agent`
- `Save as template`
- `Load template` (small dropdown)
- `Cancel running orchestration` (appears only while running; sends abort)
- `View in audit timeline →`

### States
- **Empty:** *"No orchestrations yet. Compose one above."*
- **Running:** lock the form, show a stop button, live timeline animates.
- **Failed stage:** mark the failing stage red, show its `reason`, downstream
  stages dimmed/skipped. Offer `Resume from failed stage` button.

### Data contracts (TO BE IMPLEMENTED — Stitch can mock these)
```
POST /api/orchestration/pipeline
     body: { stages: [{ agent_id, topics:[str], task:str,
                         pass_output_to_next: bool }] }
     → { run_id, stages: [{ stage_index, job: {...}, ok }] }

POST /api/orchestration/coordinator
     body: { coordinator_agent_id, scope_topics:[str], goal:str,
             allowed_subagents: [{ agent_id, max_topics:[str] }] }
     → { run_id, subhires: [{ stage_index, job, ok }],
         final_answer, mcp_calls_total }

GET  /api/orchestration/runs?limit=20
     → { runs: [{ run_id, mode, started_at, finished_at, status,
                  stages_count, final_answer_preview }] }

GET  /api/orchestration/runs/{run_id}
     → full timeline for one run
```

---

## 6. Cross-cutting components Stitch should design once and reuse

1. **`<TokenPill>`** — truncated token + copy icon (e.g. `ag_G7Ch…W` 📋).
2. **`<EventPill event="...">`** — color-coded chip for any audit event name.
3. **`<TopicChipsInput value=[] suggestions=[]>`** — comma + enter to add,
   click to remove, supports paste of `"a, b, c"`.
4. **`<AgentAvatar name="..."/>`** — circle with initials in a deterministic color from the name hash.
5. **`<StandingBadge score=0..100/>`** — pill that picks color + label from the score (trusted / established / unproven / flagged / untrusted).
6. **`<Toast type="ok|bad|warn" />`** — bottom-right notifications for revokes, runs done, errors. Auto-dismiss 4s.
7. **`<ConfirmModal />`** — for danger actions (Revoke all, Cancel run).
8. **`<SkeletonRow />`** — table loading state, 3 grey shimmer rows.

---

## 7. Copy / microcopy guidelines

- Keep language *plain and confident*, not enterprise-y.
- "Hire" not "Initiate engagement."
- "Sandboxed run" not "Container execution environment."
- For deny states: be specific. *"Topic 'salaries' is not in this agent's allowlist."*
- For empty states: tell the user the next step. *"Mint a grant above to get started."*
- For security boasts (on Dashboard compliance card): one short line each, no marketing fluff.

---

## 8. Two pieces of copy that must appear verbatim somewhere visible

For the moat narrative:
> **"Off this network an agent is an amnesiac stranger. Its memory and reputation live here, in this company's immutable history."**

For the security model:
> **"Every agent reaches the graph through MCP, inside a sandbox. Every call is scope-gated and audited. The agent has no network — only the gateway."**

---

## 9. Integration handoff — how to plug back in

When the Stitch design is ready, give me **one** of:
1. A working Stitch URL with the design + (optionally) a Stitch MCP endpoint
   I can call.
2. The exported HTML/React code and assets.
3. Screenshots + a JSON of the component tree.

I'll then:
1. Drop the new pages into `stitch_ui/` replacing the current placeholder
   pages (`marketplace.html`, `agents.html`, `dashboard.html`, new
   `orchestration.html`).
2. Wire every button / form to the existing endpoints in section 2–4 above.
3. Build the four orchestration endpoints in section 5 and wire them to the
   new page.
4. Make sure all existing 164+ tests still pass + add tests for the
   orchestration endpoints.

---

## 10. What's already built (so Stitch knows nothing is mock data)

| Area | Endpoints live | Tests |
|------|----------------|-------|
| Workspace isolation | /api/status, /api/workspace, /api/connect (admin) | 13 |
| Decisions / Brain | /api/decisions, /api/recall, /api/compiled, /api/dream/run, /api/jobs | 29 |
| GraphRAG retrieval | /api/query, /api/graph, /api/sessions/* | 17 + 11 |
| MCP (7 tools) | /api/mcp/{tools,status,config,call} | 17 |
| Integrations | /api/integrations/* | 14 (+6 skip) |
| Simulation | /api/simulation/* | 10 |
| **Marketplace** | **/api/marketplace/{agents,hire}** | 4 |
| **Grants + audit** | **/api/agent/{grants,audit,run,reputation,revoke_all,audit/search}** | 33 |
| **MCP-in-sandbox** | wired into marketplace via `via-mcp` tag | 7 |
| **Network directory** | **/api/network/companies** | 8 |
| **At-rest encryption** | opt-in via `AgentAccessStore(encrypt=True)` | 5 |

**Everything in this spec maps to a real, tested endpoint** — the Stitch
design will be rendering live data, not mocks.
