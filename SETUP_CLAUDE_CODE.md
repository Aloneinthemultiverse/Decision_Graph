# DecisionGraph + Claude Code — Production Setup

A 5-minute setup to give Claude Code (or any MCP-compatible AI) full
codebase intelligence about YOUR repo. Verified working end-to-end on
flask, fastapi, gin, express, httpx, and code-review-graph.

---

## What you get

After setup, Claude Code can call **27 tools** including:

- `find_callers(name)` — who calls this function?
- `blast_radius(path)` — what files break if I change this?
- `causal_radius(path)` — 5-layer impact: blast + ADRs + PRs + commits + docs
- `triage_pr(changed_files)` — 0-100 risk score with merge-order hint
- `topology()` — god-nodes, surprising cross-folder edges, dead code
- `rationales(symbol)` — surface `# WHY:`/`# HACK:`/`# SAFETY:` near code
- `suggested_questions()` — bootstrap onboarding questions
- `federated_*` — cross-repo queries across multiple workspaces
- `export_graph_html(out)` — interactive offline visualization
- 18 more (decisions, agents, ingest, etc.)

Cold-start: **~5 seconds**. Per-tool latency: **1–50 ms** for structural
queries, ~1 s for LLM-backed queries.

---

## Step 1 — Install DecisionGraph

```bash
git clone <this-repo>
cd decisiongraph_v2
pip install -r requirements.txt
```

## Step 2 — Index YOUR repo

Pick the repo you want Claude Code to know about:

```bash
cd /path/to/your-repo
python -m decisiongraph.code_graph_cli install-hook \
    --repo-name your-org/your-repo
```

This writes a git `post-commit` hook. Every commit will auto-rebuild
`.dg_code_graph.db` at your repo root (typically < 1 s).

To trigger the first build immediately:

```bash
git commit --allow-empty -m "initial graph build"
```

Verify:

```bash
python -c "from decisiongraph import code_graph as cg; \
print(cg.stats('.dg_code_graph.db'))"
```

You should see file/symbol/calls counts > 0.

## Step 3 — Wire Claude Code

Drop this into your Claude Code settings (`.mcp.json` in the repo root,
or your global Claude Code config):

```json
{
  "mcpServers": {
    "decisiongraph": {
      "command": "python",
      "args": ["-m", "decisiongraph.mcp_server"],
      "cwd": "/path/to/your-repo",
      "env": {
        "PYTHONPATH": "/path/to/decisiongraph_v2",
        "PYTHONIOENCODING": "utf-8",
        "DG_CODE_GRAPH_DB": "/path/to/your-repo/.dg_code_graph.db"
      }
    }
  }
}
```

On Windows, use forward slashes in paths.

## Step 4 — Test

Open Claude Code in your repo and ask:

> "Use the decisiongraph MCP tools to tell me: which symbol in this
> repo has the most callers, and what would break if I changed it?"

Claude will call `topology` → `find_callers` → `blast_radius` and
answer with concrete file paths and counts. **If you get an answer
referencing real files in your repo, setup is complete.**

---

## Performance numbers (verified)

| Operation | Time |
|---|---|
| MCP server cold-boot | ~5 s |
| `find_callers` | < 5 ms |
| `blast_radius` | < 10 ms |
| `triage_pr` (5 files) | < 50 ms |
| `topology` | < 20 ms |
| `causal_radius` | ~25 ms (with 200+ decisions) |
| Full graph rebuild on a 1000-file repo | 2–5 s |

## Configuration

| Env var | Effect | Default |
|---|---|---|
| `DG_CODE_GRAPH_DB` | Explicit path to the SQLite db | auto-discovered |
| `LLM_BASE_URL` | LLM gateway URL (Anthropic-format) | `https://api.anthropic.com` |
| `LLM_API_KEY` | API key for above | (empty) |
| `LLM_MODEL` | Model name | `claude-sonnet-4-6` |
| `EMBED_MODEL` | Sentence-transformer for semantic search | `all-MiniLM-L6-v2` |

DB auto-discovery order:
1. `DG_CODE_GRAPH_DB` env (explicit override, no existence check)
2. `./.dg_code_graph.db` in CWD
3. `{storage_dir}/code_graph.db` (workspace-style)
4. `{storage_dir}/../.dg_code_graph.db`

## Uninstall

```bash
cd /path/to/your-repo
python -m decisiongraph.code_graph_cli uninstall-hook
rm .dg_code_graph.db
```

The hook script preserves any pre-existing post-commit content you had.

## Troubleshooting

**Tools return `0 files, 0 symbols`**
The MCP server isn't finding your db. Check the `cwd` and
`DG_CODE_GRAPH_DB` in your `.mcp.json`. Run `python -c "from
decisiongraph.mcp_server import _cg_db_path; from
decisiongraph.core import DecisionGraph; print(_cg_db_path(DecisionGraph()))"`
inside Claude Code's spawn env to see what path it's checking.

**`ingest_document` returns 404 / connection error**
Your `LLM_BASE_URL` is unreachable. Either start your local gateway
or set `LLM_BASE_URL` to a working endpoint. Structural tools
(`find_callers`, `blast_radius`, etc.) don't need the LLM and will
keep working.

**Claude Code says "decisiongraph server is not responding"**
Server boot takes ~5 s on first launch. If it's longer, check the
post-commit hook actually ran by running it manually:

```bash
python -m decisiongraph.code_graph_cli rebuild --repo-path .
```

---

## Verified end-to-end

This setup was verified on `pallets/flask @ a29f88ce6` with these
real measurements:

- 82 files, 1545 symbols, 5062 calls, 654 imports, 112 inherits, 1 rationale
- After agent edits ctx.py (`mark_dirty` method added):
  - graph auto-rebuilds (1544 → 1545 symbols)
  - `blast_radius` reflects new symbol
  - `triage_pr` correctly scores MEDIUM with merge-last hint
  - `causal_radius` surfaces ADRs + PRs + commits + docs
- Mutation test (rename `add_url_rule` god-node) → `triage_pr`
  flags MEDIUM god-node touch; pytest confirms 5+ real test failures
- 27/27 MCP tools dispatch successfully via real JSON-RPC stdio
- Cold-boot 5 s, per-tool latency 1–50 ms
- F1 benchmark: 0.59 git-truth (recall ≈ 1.0)
