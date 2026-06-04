# Enterprise Agentic OS — Design Notes

*Planning doc. The environment a company's AI agents operate inside —
persistent memory, learned skills, governance. Gets smarter every task.*

---

## The goal

A proper Enterprise OS: the environment AI coding agents run inside.
It remembers everything (code + decisions + habits) and gets smarter over time.

## Three faces (who uses it)

1. **Developer** — works, uses agents to write code
2. **Company admin** — governs the OS for a whole team
3. **The platform** — not locked to one company; anyone can run it

## The three-layer stack

```
┌─────────────────────────────┐
│  KERNEL                      │  ← runs agents, gives them memory+tools, supervises
├─────────────────────────────┤
│  AGENTS  (ECC: 63 agents,    │  ← the workers
│           249 skills)        │
├─────────────────────────────┤
│  MEMORY  ✅ HAVE (DG)         │  ← knowledge graph + decisions + code graph
└─────────────────────────────┘
```

- **Memory** = DG (have it)
- **Agents** = ECC (mostly have, via integration)
- **Kernel** = TO BUILD (the coordinator)

---

## The Kernel — 4 jobs

When a task arrives, the kernel does these in order:

```
TASK → 1. DISPATCH → 2. LOAD → 3. GRANT → 4. RUN+LOG
```

### Job 1 — DISPATCH (pick the agent)
A router looks at the task and picks the right agent(s).
- Level 1: keyword match
- Level 2: ask the LLM "which agent fits?"
- Level 3 (our edge): **consult DG first** — "this touches god-nodes → use the careful reviewer"

### Job 2 — LOAD (give the agent the right memory slice)
Don't dump the whole graph. Load only the slice relevant to THIS task.
- Ask DG: `causal_radius`, `blast_radius`, relevant instincts
- Pack into the agent's context within a token budget
- **Delivery split:**
  - SEMANTIC (slow, ~13s) → **CAG**: preload once into context
  - STRUCTURAL (fast, ~5-10ms) → **live calls**: agent calls when needed

### Job 3 — GRANT (give only allowed tools)
Hand the agent a scoped, audited toolset. Allow some, forbid others.
- ✅ read graph, edit /src, run tests
- ❌ delete files, touch /secrets, push to main
- **Already built** = AgentNet (grant tokens, scope-gate, audit log)

### Job 4 — RUN + LOG (execute, watch, save the loop)
- RUN: agent works
- WATCH: every tool call logged + gated
- SAVE (close the loop): push results back to DG
  - new code → `update_files`
  - decisions → `store_decision`
  - lessons → instinct
- **The loop is the whole point:** each task makes the next start smarter.
  Tool = forgets you. OS = remembers + improves.

---

## RAG vs CAG (memory delivery)

- **RAG** = search + fetch relevant bits per question (good for huge knowledge)
- **CAG** = preload all relevant knowledge into context ONCE (good when it fits)

**Decision:** CAG the slow semantic layer (load once at session start),
keep the fast structural tools as live calls. The 13s query never happens
during work.

---

## Integration: DG ↔ Agents (ECC)

Core idea — agents touch memory at two moments:
```
BEFORE acting → READ from DG ("what do I need to know?")
AFTER acting  → WRITE to DG  ("here's what I did / learned")
```

The 3 thin seams (no codebase merge — they talk via MCP + hooks):
1. **Skills reference DG tools** (markdown, zero code) — "before editing, call blast_radius"
2. **session-start hook pulls DG context** (~10 lines in ECC's session-start.js)
3. **session-end hook writes learned habits → DG** (redirect ECC's instinct extractor to store_decision)

---

## What we already have

- ✅ Memory: knowledge graph + decisions + 32-lang code graph
- ✅ Structural tools: find_callers, blast_radius, triage_pr, topology, causal_radius
- ✅ update_files (instant incremental graph update after edits)
- ✅ Governance: AgentNet (grants, scope-gate, audit, multi-tenant)
- ✅ MCP transport (stdio + HTTP gateway)

## What's to build

- ⬜ CAG layer (precompile semantic context blob, inject at session start)
- ⬜ The kernel (dispatch → load → grant → run+log)
- ⬜ ECC integration seams (2 hook edits + skills)
- ⬜ Instinct/pattern distillation (the ECC idea, grounded in DG facts)
- ⬜ Admin dashboard / RBAC / SSO (enterprise hardening)

---

## CAG integration (designed)

### The cache blob = the existing blueprint.md
DG already generates a blueprint markdown at ingest (~130KB for V).
That IS the CAG cache — no new artifact needed.

### Tier the blueprint (don't preload all 130KB / ~32k tokens)
- ALWAYS-CAG: small repo-overview core (~3k tokens) — preloaded every session
- ON-DEMAND: detailed folder/module sections — loaded only for the area
  the task touches (blueprint is already structured by folder, easy to slice)

### TWO-TIER CAG (the key design — like human memory)
```
CAG #1  KERNEL-CAG (working memory — hot, short-term)
  holds: THIS session's activity — recent edits, decisions, tool results,
         the agent scratchpad + the loaded blueprint slice
  fast · ephemeral · lives with the running agent
        │
        │  SESSION ENDS → digest + consolidate
        ▼
CAG #2  DG-CAG (long-term memory — stable, persistent)
  holds: the blueprint (repo overview + module summaries) — distilled knowledge
  stable · persistent · the foundation
```

Brain analogy:
- KERNEL-CAG = working memory (this session, forgotten unless consolidated)
- session-end consolidation = sleep (move what mattered to long-term)
- DG-CAG = long-term memory (stable knowledge)

### The two flows
```
DURING work:  agent reads/writes KERNEL-CAG (instant). Slow DG never blocks.
SESSION END:  kernel digests its CAG → writes to DG → DG-CAG (blueprint) refreshes
```

DG is touched only twice per session: load slice at start, consolidate at end.
Everything in between is the fast hot cache.

### Still open (next discussion)
- HOW the kernel digests its CAG at session end (what gets distilled, what's dropped)
- WHERE each cache physically lives + how loading stays instant

---

# BUILD PLAN — Phases & Subphases

## Division of labor (what we build vs reuse)
- **REUSE (don't build):** ECC repo → agents (63), skills (249), hooks
- **HAVE (built):** DG → memory, code graph, governance (AgentNet), MCP, update_files
- **BUILD (new):** the kernel · two-tier CAG · ECC↔DG integration seams · consolidation

---

## PHASE 0 — Foundation lock (make the base solid)
*Goal: DG + ECC both installed and reachable, baseline proven.*
- 0.1  Pin DG production: server stable, ngrok/host stable, MCP gateway has all
       structural tools (DONE this session)
- 0.2  Install ECC into a test Claude Code; confirm its skills/hooks load
- 0.3  Confirm both coexist: ECC skills available + DG MCP tools available in
       the same session
- 0.4  Pick the pilot repo (V) + one pilot workspace
- **Exit:** an agent in Claude Code can call DG tools AND has ECC skills, same session

## PHASE 1 — Integration seams (connect ECC ↔ DG)
*Goal: agents READ from DG before acting, WRITE to DG after. No codebase merge.*
- 1.1  **Seam 1 (zero code):** write 1 ECC skill/rule — "before editing, call
       get_codebase_context + blast_radius; after editing, call update_files"
- 1.2  Test Seam 1 on V: does the agent actually use DG tools before coding?
- 1.3  **Seam 2:** edit ECC `session-start.js` (~10 lines) → pull DG onboarding
       brief, inject into session context
- 1.4  **Seam 3:** edit ECC `evaluate-session.js` → redirect learned patterns to
       DG `store_decision` instead of flat files
- **Exit:** an ECC agent auto-loads DG context at start, uses DG tools mid-task,
  writes lessons to DG at end

## PHASE 2 — CAG layer (the two-tier cache)
*Goal: fast memory delivery, slow DG never blocks the agent.*
- 2.1  **DG-CAG:** serve the blueprint as a tiered blob —
       small overview (always) + folder sections (on-demand). New MCP tool:
       `get_context_pack(task_files)` → returns the right slice
- 2.2  **KERNEL-CAG:** a hot per-session scratchpad store (recent edits/decisions/
       tool results) the agent reads/writes during work
- 2.3  Wire CAG into Seam 2: session-start loads DG-CAG slice into KERNEL-CAG
- 2.4  Measure: confirm no 13s blocking call happens during work
- **Exit:** agent works entirely against the hot cache; DG hit only at start/end

## PHASE 3 — The kernel (the coordinator)
*Goal: a thing that runs a task through all 4 jobs.*
- 3.1  **Job 1 Dispatch:** task → router → pick ECC agent (start: LLM router;
       later: consult DG risk to pick careful vs fast agent)
- 3.2  **Job 2 Load:** kernel calls get_context_pack → fills KERNEL-CAG
- 3.3  **Job 3 Grant:** kernel mints an AgentNet scoped grant for the agent
- 3.4  **Job 4 Run+Log:** run agent, audit every call, collect results
- 3.5  Minimal kernel = thin CLI/service that chains 3.1→3.4 for one task
- **Exit:** `kernel run "add feature X"` → picks agent, loads memory, scopes tools,
  runs, logs — end to end

## PHASE 4 — Consolidation (the "sleep" step)
*Goal: session end digests the hot cache into long-term memory + distills lessons.*
- 4.1  **Digest:** at session end, summarize KERNEL-CAG → "what mattered"
- 4.2  **Structural write-back:** update_files for edited files (instant)
- 4.3  **Decision write-back:** store the digest as decisions in DG
- 4.4  **Instinct distillation (the ECC idea, deeper):** scan recent decisions for
       recurring patterns → confidence-scored, decaying instincts, grounded in
       DG code facts. Reuse existing run_dream_cycle as the trigger
- 4.5  **DG-CAG refresh:** blueprint updates so next session starts smarter
- **Exit:** repeated sessions visibly compound — the OS gets smarter each task

## PHASE 5 — Enterprise hardening (the 3 faces)
*Goal: a company admin can run this for a whole team, safely.*
- 5.1  Team RBAC on top of tenant isolation (who in the team can do what)
- 5.2  SSO / org auth
- 5.3  Admin dashboard: review audit log, approve/reject instincts, see usage
- 5.4  Per-team scoped grants + quotas
- **Exit:** admin onboards a team, sets permissions, watches the audit

## PHASE 6 — OS surface & launch
*Goal: a sellable Enterprise Agent OS.*
- 6.1  Unified console: knowledge graph + instinct catalog + governance, one UI
- 6.2  Cross-repo / org-wide knowledge (federation we built) + org-wisdom distill
- 6.3  Instinct/skill marketplace (teams share learned patterns) — extends AgentNet
- 6.4  Packaging, docs, pricing/seats
- **Exit:** a company can adopt the OS end-to-end

---

## Dependency order
```
PHASE 0 (foundation)
   └─► PHASE 1 (seams) ──┐
   └─► PHASE 2 (CAG) ────┤
                         └─► PHASE 3 (kernel) ──► PHASE 4 (consolidation)
                                                      └─► PHASE 5 (enterprise)
                                                             └─► PHASE 6 (launch)
```
Phases 1 and 2 can run in parallel. Kernel needs both. Everything after is linear.

## Smallest first win
Phase 0 + Phase 1.1–1.2 = an agent that uses DG context before coding, ZERO new
code (just one ECC skill). That's the cheapest proof the integration works.
