# Plug your IDE's AI into your DecisionGraph

Any IDE / agent that speaks **MCP over HTTP** can plug into your DG. The
agent gets a bearer token scoped to specific topics, calls tools through the
gateway, and every read is audited.

This is the "Shape B" play: don't build a coding agent — make Cursor /
Claude Code / Cline / Continue / Windsurf / Zed / your-tool-of-choice into a
*codebase-aware* coding agent by giving them your institutional memory as a
tool.

---

## 1 · Get a connection bundle

In the DG UI:

1. Open `/marketplace` (or the **MCP Server** tab inside `/app`)
2. Click **Connect as owner** (if it's you) or **Connect scoped agent** (if it's a third-party tool you want to limit)
3. Fill in:
    - Name (anything; shows in audit log)
    - Topics (allowed list, for scoped only)
    - TTL (24h is a sane default)
4. Click **Generate connection**
5. Copy the bundle: URL, bearer header, and `claude mcp add ...` command

The URL looks like:
```
https://<your-host>.ngrok-free.dev/api/mcp/v1/owner/<workspace_id>
```

The header looks like:
```
Authorization: Bearer ow_xxxxxxxxxxxxxxxxxxxx
```

---

## 2 · Claude Code (Anthropic)

```bash
claude mcp add decisiongraph-owner \
  --transport http \
  --header "Authorization: Bearer ow_XXXXXXXXXXXX" \
  --scope user \
  https://<your-host>/api/mcp/v1/owner/<workspace_id>
```

Then **fully quit and reopen** Claude Code. The new session has the DG
tools natively in its palette: `ask`, `recall_entity`, `get_codebase_context`,
`ingest_repo`, `forecast`, etc.

Ask Claude:
> "Use the decisiongraph tools to look up institutional context for `src/auth/login.py` — I'm refactoring the login flow."

It'll call `get_codebase_context(file_path="src/auth/login.py", intent="refactoring login flow")` and ground its answer in the DG's prior decisions.

---

## 3 · Cursor

Cursor supports MCP via its settings. Open **Settings → MCP** and add:

```json
{
  "mcpServers": {
    "decisiongraph": {
      "url": "https://<your-host>/api/mcp/v1/owner/<workspace_id>",
      "headers": {
        "Authorization": "Bearer ow_XXXXXXXXXXXX"
      }
    }
  }
}
```

Reload Cursor. The DG tools appear in the agent's tool list. Cursor's agent
can now call `get_codebase_context` automatically when working in a file
that has prior DG context.

---

## 4 · Cline / Continue / Other VS Code extensions

Cline (formerly Claude Dev) and Continue both support MCP. Each has a
`mcpServers` config in their settings JSON. Use the same shape as Cursor's
config above.

For Continue, the config file is usually at:
`~/.continue/config.json`

For Cline, you'd open VS Code settings → MCP → add the server.

---

## 5 · Auto-capture decisions from commits

Drop the included git hook into your repo to auto-store every commit as a
decision in DG memory:

```bash
cd /path/to/your/repo
cp <DG_REPO>/agentnet/hooks/post-commit .git/hooks/post-commit
chmod +x .git/hooks/post-commit
```

Set three env vars (in your shell profile, or a `.env` your shell sources):

```bash
export DG_URL="https://<your-host>"
export DG_TOKEN="ow_XXXXXXXXXXXX"
export DG_WS="<workspace_id>"
```

From now on, every `git commit` posts the commit subject + body + file list
to the DG as a `[repo:<name>] commit · <sha> · <subject>` decision. Skip with
`git commit --no-verify`.

The hook **never blocks the commit** even if the DG is down — it just warns
and exits clean.

---

## 6 · One-time repo ingest

Don't want to wait for `N` commits to build memory? Pre-seed it. From your
DG owner session (Claude Code, Cursor, etc.):

> "Use ingest_repo on `/path/to/myrepo`."

This walks the repo and folds in:
- READMEs / CONTRIBUTING / CHANGELOG
- All ADRs (`docs/adr/`, `doc/decisions/`)
- Package manifests (`package.json`, `pyproject.toml`, …)
- CODEOWNERS
- Module-level docstrings from `.py` files
- The last 200 commit messages as a timeline

Returns a summary: `stored=187, adr=12, doc=23, commit=200, ...`

---

## 7 · The end-to-end loop

```
git commit                          → DG captures the decision (B2 hook)
                                       ↓
                                    [repo:foo] commit · 7d2a3b1 · "switch JWT lib"
                                       ↓
6 months later, editing auth.py:
                                       ↓
Cursor calls get_codebase_context     → finds that decision
                                       ↓
Cursor's prompt to its LLM:
   "INSTITUTIONAL CONTEXT (from DG):
     [1] [repo:foo] commit 7d2a3b1: switched JWT lib from x to y
         → because of CVE-2024-xxxx and we wanted a stricter audience check
   Use this context when answering. Cite specific items by [N]."
                                       ↓
Suggestion includes the reasoning, cites the prior decision
                                       ↓
You merge a new commit
                                       ↓
Hook captures THIS decision too — the cycle compounds
```

That's the play. Your IDE's AI gets smarter about *your* codebase, every
time anyone commits.
