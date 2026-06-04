# DecisionGraph — Backup & Restore

Verified procedure for backing up and restoring all DG state.

---

## What needs backing up

DG stores state in **three places**:

| What | Where | Format | Sensitivity |
|---|---|---|---|
| **Code graph** | `<repo>/.dg_code_graph.db` | SQLite | Per-repo. Cheap to rebuild from source — *optional* to back up. |
| **Per-workspace decisions + knowledge graph** | `storage/workspaces/<ws_id>/personal/` | pickle + SQLite | **Critical.** Contains ADRs, decisions, ingested docs. Lose this → lose institutional memory. |
| **Multi-tenant company state** | `storage/companies/<company_id>/` | pickle + SQLite | **Critical** if you use the multi-tenant feature. |
| **Settings / agent grants / audit** | `storage/agent_audit.jsonl`, `storage/agents.json` | JSONL / JSON | **Critical** for security/compliance. |
| **Discussion sessions** | `storage/<ws>/personal/discussions/sessions.pkl` | pickle | **Critical** if discussions matter. |

`.dg_code_graph.db` is regenerable (just re-run `code_graph_cli rebuild`), so optional.

---

## Backup — one-shot tarball

The whole `storage/` directory is portable. Tar it up:

```bash
# Unix
tar -czf dg-backup-$(date +%F).tar.gz storage/

# Windows PowerShell
Compress-Archive -Path storage -DestinationPath ("dg-backup-{0:yyyy-MM-dd}.zip" -f (Get-Date))
```

Stop the DG server first (`Ctrl-C`) so no in-flight SQLite writes are mid-flush.

If you can't stop the server, use SQLite's `.backup` for live consistency:

```bash
for db in $(find storage/ -name "*.db"); do
    sqlite3 "$db" ".backup '$db.snapshot'"
done
# then tar up the .snapshot files instead
```

---

## Restore

1. Stop any running DG processes
2. Move existing `storage/` aside: `mv storage storage.old`
3. Extract: `tar -xzf dg-backup-YYYY-MM-DD.tar.gz`
4. Verify the directory tree:

```bash
ls storage/
# expect:  workspaces/  companies/  agent_audit.jsonl  agents.json
```

5. Start the server: `python server.py`
6. Verify in the UI that your workspaces appear at `/api/workspace`

---

## Per-workspace backup (move ONE workspace between machines)

```bash
WS_ID=p4jIEgrAJd33s3qH-DZbhQ
tar -czf ws-$WS_ID.tar.gz storage/workspaces/$WS_ID/
```

To restore on another machine, drop the tarball into the destination's `storage/workspaces/` directory and extract.

The MCP server and `/api/query` will auto-pick up the new workspace.

---

## Verified restore procedure

Tested 2026-05-27 on Windows. Steps:

1. Workspace `p4jIEgrAJd33s3qH-DZbhQ` had **216 decisions** (15 manually + 200 from concurrency test + 1 from this verification).
2. Tarred → 1.8 MB archive.
3. Stopped server, moved `storage/` to `storage.bak/`, extracted backup.
4. Restarted server → workspace re-loaded → 216 decisions present.
5. Ran `/api/query "what is in the graph?"` → returned grounded 1.5KB answer.

**Restore verified end-to-end with zero data loss.**

---

## Disaster recovery checklist

If `storage/` is gone:

- [ ] **Code graphs** → re-run `python -m decisiongraph.code_graph_cli rebuild` in each repo (~5s per 1000 files)
- [ ] **Workspaces / decisions** → restore from the most recent `dg-backup-*.tar.gz`. If no backup, decisions are lost.
- [ ] **Ingested documents** → re-ingest via `/api/ingest/file` (idempotent on file content; same docs produce same nodes)
- [ ] **Agent grants** → revoked grants stay revoked even without backup (they expired); active grants are lost — re-issue from `/api/agent/grants`
- [ ] **Discussion sessions** → lost. Discussion summaries are folded into decisions on `end_session()` so partially preserved IF you backed up decisions.

---

## Recommended cadence

| Concern | Cadence |
|---|---|
| Personal dev usage | Weekly tar of `storage/workspaces/<your_ws>/` |
| Multi-tenant production | Daily, with off-site copy |
| Compliance / audit | Append-only `storage/agent_audit.jsonl` — back up at least daily, never modify |
| Code graphs | Don't bother; rebuild on demand |

## What's NOT backed up (intentional)

| Item | Why |
|---|---|
| `.bench/` | Test artifacts; regenerable |
| `_*.py` (root-level test scripts) | Test scaffolding; not part of runtime |
| `node_modules/`, `__pycache__/` | Build artifacts |
| `.env` | Credentials — back up SEPARATELY in a secrets manager, not in repo backups |
