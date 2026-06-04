"""Unified MCP tool surface — every meaningful DecisionGraph capability,
exposed as MCP tools so any MCP-capable agent (Claude/Cursor/custom) can
drive the app over the protocol.

Two modes share this registry:
  * OWNER  — workspace owner has full access (ingest, create company, dream,
             simulation, write decisions). Used when the human / their own
             assistant connects.
  * AGENT  — scoped hired agent (Step-1 grant token). Read-only by default,
             topic-scoped, and only the read tools (`ask`, `recall`, query,
             get_graph, get_compiled, etc.) are reachable. Write/ingest tools
             return an error.

Each entry: { schema, handler(ws, args, ctx, owner) -> dict,
              owner_only: bool, needs_scope: bool }
"""
from __future__ import annotations

import os
import json
import time
from datetime import datetime


def _err(msg, **extra):
    return {"error": msg, **extra}


# ── 1. INGEST ──────────────────────────────────────────────────────────────
def _h_ingest_pdf(ws, args, ctx, owner):
    """Queue async PDF ingest via the durable job queue. Returns job_id so
    callers can poll job_status — PDF ingest can take minutes (LLM extracts
    triples per chunk) and would blow MCP client timeouts if run sync."""
    path = (args.get("path") or "").strip()
    if not path or not os.path.isfile(path):
        return _err(f"file not found: {path}")
    jid = ctx["JOBS"].enqueue(
        "ingest", {"kind": "pdf", "path": path, "workspace": ws.token,
                   "company_id": args.get("company_id")},
        workspace=ws.token)
    return {"job_id": jid, "queued": True,
            "note": "Poll job_status(job_id) — PDF ingest can take 2-5 min."}


def _h_ingest_url(ws, args, ctx, owner):
    url = (args.get("url") or "").strip()
    if not url:
        return _err("url required")
    jid = ctx["JOBS"].enqueue(
        "ingest", {"kind": "url", "url": url, "workspace": ws.token,
                   "company_id": args.get("company_id")},
        workspace=ws.token)
    return {"job_id": jid, "queued": True}


def _h_ingest_youtube(ws, args, ctx, owner):
    url = (args.get("url") or "").strip()
    if not url:
        return _err("url required")
    jid = ctx["JOBS"].enqueue(
        "ingest", {"kind": "youtube", "url": url, "workspace": ws.token,
                   "company_id": args.get("company_id")},
        workspace=ws.token)
    return {"job_id": jid, "queued": True}


def _h_ingest_media(ws, args, ctx, owner):
    path = (args.get("path") or "").strip()
    if not path or not os.path.isfile(path):
        return _err(f"media file not found: {path}")
    jid = ctx["JOBS"].enqueue(
        "ingest", {"kind": "media", "media_path": path,
                   "workspace": ws.token,
                   "company_id": args.get("company_id")},
        workspace=ws.token)
    return {"job_id": jid, "queued": True}


def _h_run_python(ws, args, ctx, owner):
    """Execute Python in a fresh ephemeral sub-sandbox (--network none, RO fs,
    256m mem, 1 CPU, hard timeout). Returns stdout/stderr/exit_code. Same
    isolation guarantees as the main agent sandbox — code cannot reach the
    host or any network. Available to scoped agents AND owners; the scope gate
    doesn't apply because no DG data is read by this tool (it's pure
    computation)."""
    code = args.get("code") or ""
    if not code.strip():
        return _err("code required")
    timeout = max(1, min(int(args.get("timeout_seconds") or 30), 120))

    import shutil as _sh
    from .agent_sandbox import docker_available, SandboxConfig
    if not docker_available():
        return _err("docker not available")

    import tempfile, subprocess as _sp
    work = tempfile.mkdtemp(prefix="dg_pyexec_")
    try:
        with open(os.path.join(work, "exec.py"), "w", encoding="utf-8") as f:
            f.write(code)
        cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m",
            "--memory", "256m",
            "--cpus", "1.0",
            "--pids-limit", "64",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", f"{work}:/sandbox:ro",
            "-w", "/sandbox",
            "python:3.12-slim",
            "python", "/sandbox/exec.py",
        ]
        try:
            r = _sp.run(cmd, capture_output=True, text=True, timeout=timeout)
        except _sp.TimeoutExpired:
            return _err(f"code execution exceeded {timeout}s hard timeout")
        return {
            "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout": (r.stdout or "")[:8000],
            "stderr": (r.stderr or "")[:4000],
            "timeout_seconds": timeout,
        }
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
    finally:
        _sh.rmtree(work, ignore_errors=True)


# ── INTER-AGENT MESSAGE TOOLS (S3 — only active inside dialog runs) ───────
def _msg_ctx(ctx):
    """Returns (bus, run_id, agent_name) or (None, None, None) if not in a run."""
    bus = ctx.get("bus"); run_id = ctx.get("run_id")
    agent = ctx.get("agent_name") or "unknown"
    if not bus or not run_id:
        return None, None, None
    return bus, run_id, agent


def _h_post_message(ws, args, ctx, owner):
    bus, run_id, agent = _msg_ctx(ctx)
    if not bus:
        return _err("post_message is only available inside an orchestration dialog run")
    to = (args.get("to") or "all").strip()
    text = (args.get("text") or "").strip()
    if not text:
        return _err("text required")
    kind = (args.get("kind") or "msg").strip() or "msg"
    rec = bus.post(run_id, agent, to, text[:8000], kind=kind)
    return {"ok": True, "id": rec["id"], "ts": rec["ts"], "to": rec["to"],
            "kind": rec["kind"]}


def _h_read_messages(ws, args, ctx, owner):
    bus, run_id, agent = _msg_ctx(ctx)
    if not bus:
        return _err("no active orchestration run")
    since = float(args.get("since_ts") or 0)
    limit = int(args.get("limit") or 50)
    msgs = bus.read(run_id, agent, since_ts=since, limit=limit)
    return {"messages": msgs, "count": len(msgs)}


def _h_wait_for_message(ws, args, ctx, owner):
    bus, run_id, agent = _msg_ctx(ctx)
    if not bus:
        return _err("no active orchestration run")
    since = float(args.get("since_ts") or 0)
    timeout = float(args.get("timeout_seconds") or 20)
    msgs = bus.wait_for(run_id, agent, since_ts=since, timeout_s=timeout)
    return {"messages": msgs, "count": len(msgs)}


# ── PEER-TOOL MESH (S5) ──────────────────────────────────────────────────
def _h_register_peer_tool(ws, args, ctx, owner):
    bus, run_id, agent = _msg_ctx(ctx)
    if not bus:
        return _err("only available inside an orchestration run")
    name = (args.get("name") or "").strip()
    if not name:
        return _err("tool name required")
    decl = bus.declare_tool(
        run_id, agent, name,
        description=(args.get("description") or ""),
        input_schema=(args.get("input_schema") or {}))
    return {"ok": True, "declared": decl}


def _h_list_peer_tools(ws, args, ctx, owner):
    bus, run_id, agent = _msg_ctx(ctx)
    if not bus:
        return _err("only available inside an orchestration run")
    tools = [t for t in bus.list_tools_for_run(run_id) if t["provider"] != agent]
    return {"tools": tools, "count": len(tools)}


def _h_call_peer_tool(ws, args, ctx, owner):
    """Call a tool provided by another agent in the same run. Posts a
    tool_request message; blocks until matching tool_response arrives or
    timeout. Same scope+audit guarantees as everything else (every message
    on the bus is audited)."""
    bus, run_id, agent = _msg_ctx(ctx)
    if not bus:
        return _err("only available inside an orchestration run")
    provider = (args.get("provider") or "").strip()
    tool_name = (args.get("tool_name") or "").strip()
    inp = args.get("args") or {}
    timeout = float(args.get("timeout_seconds") or 30)
    if not provider or not tool_name:
        return _err("provider and tool_name required")
    import json as _j, time as _t, uuid as _uuid
    req_id = "req_" + _uuid.uuid4().hex[:10]
    bus.post(run_id, agent, provider,
              _j.dumps({"req_id": req_id, "tool_name": tool_name, "args": inp}),
              kind="tool_request")
    deadline = _t.time() + max(2.0, min(timeout, 120.0))
    since_ts = 0.0
    while _t.time() < deadline:
        wait_s = max(0.5, min(2.0, deadline - _t.time()))
        msgs = bus.wait_for(run_id, agent, since_ts=since_ts, timeout_s=wait_s)
        for m in msgs:
            if m.get("kind") == "tool_response" and m.get("from") == provider:
                try:
                    body = _j.loads(m.get("text", "{}"))
                except Exception:
                    continue
                if body.get("req_id") == req_id:
                    return {"ok": True, "result": body.get("result"),
                            "from": provider, "req_id": req_id}
            since_ts = max(since_ts, m.get("ts", 0))
    return _err(f"peer tool call timed out after {timeout}s")


def _h_list_peers(ws, args, ctx, owner):
    bus, run_id, agent = _msg_ctx(ctx)
    if not bus:
        return _err("no active orchestration run")
    others = [p for p in bus.peers(run_id) if p != agent]
    return {"peers": others, "self": agent, "run_id": run_id}


def _h_ingest_repo(ws, args, ctx, owner):
    """Ingest a local git repository's docs + ADRs + commits into the DG.
    Owner-only because it touches a filesystem path."""
    from .codebase import ingest_repo
    path = (args.get("path") or "").strip()
    if not path:
        return _err("path required")
    include_commits = bool(args.get("include_commits", True))
    include_files = bool(args.get("include_files", True))
    return ingest_repo(ws.dg, path,
                       include_commits=include_commits,
                       include_files=include_files)


def _h_ingest_github(ws, args, ctx, owner):
    """Ingest a public github.com URL into the DG. Full pipeline: AST chunks +
    folder/repo rollups + call-graph edges + recent PRs + incremental cache."""
    from .codebase import ingest_github_url, _parse_github_url
    repo_url = (args.get("repo_url") or "").strip()
    if not repo_url:
        return _err("repo_url required")
    if not _parse_github_url(repo_url):
        return _err("must be a public github.com URL")
    try:
        return ingest_github_url(
            ws.dg, repo_url,
            branch=(args.get("branch") or None),
            include_code_summaries=bool(args.get("include_code", True)),
            use_ast=bool(args.get("use_ast", True)),
            include_call_edges=bool(args.get("include_call_edges", True)),
            include_hierarchical=bool(args.get("include_hierarchical", True)),
            include_prs=bool(args.get("include_prs", True)),
            incremental=bool(args.get("incremental", True)))
    except Exception as e:
        return _err(f"github ingest failed: {e}")


def _h_get_codebase_context(ws, args, ctx, owner):
    """Return the most relevant DG decisions for an IDE/agent currently
    editing `file_path` with `intent`. Use this BEFORE answering coding
    questions so the answer is grounded in project history."""
    from .codebase import get_codebase_context
    return get_codebase_context(
        ws.dg,
        file_path=args.get("file_path"),
        intent=args.get("intent"),
        repo_name=args.get("repo_name"),
        limit=int(args.get("limit") or 10))


# ── Structural code-graph tools (HTTP gateway) ──────────────────────────────
def _cg_db(ws) -> str:
    """Resolve the code_graph.db for this workspace."""
    import os
    return os.path.join(ws.dg.storage_dir, "code_graph.db")


def _cg_repo(ws, args) -> str:
    """Best-effort repo name: explicit arg, else most-populated in the graph."""
    repo = args.get("repo")
    if repo:
        return repo
    import sqlite3, os
    db = _cg_db(ws)
    if os.path.exists(db):
        try:
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT repo FROM files GROUP BY repo "
                "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
            conn.close()
            if row: return row[0]
        except Exception:
            pass
    return None


def _h_cg_find_callers(ws, args, ctx, owner):
    from . import code_graph as cg
    return {"name": args.get("name"),
            "callers": cg.find_callers(_cg_db(ws), args.get("name", ""),
                                        repo=_cg_repo(ws, args),
                                        limit=int(args.get("limit") or 50))}


def _h_cg_blast_radius(ws, args, ctx, owner):
    from . import code_graph as cg
    return cg.blast_radius(_cg_db(ws), args.get("path", ""),
                            repo=_cg_repo(ws, args),
                            max_depth=int(args.get("max_depth") or 3))


def _h_cg_triage_pr(ws, args, ctx, owner):
    from . import code_graph as cg
    return cg.triage_pr(_cg_db(ws), args.get("changed_files") or [],
                         repo=_cg_repo(ws, args),
                         max_hops=int(args.get("max_hops") or 3))


def _h_cg_topology(ws, args, ctx, owner):
    from . import code_graph as cg
    return cg.analyze_topology(_cg_db(ws), repo=_cg_repo(ws, args),
                                top_god=int(args.get("top_god") or 15),
                                top_surprise=int(args.get("top_surprise") or 15))


def _h_cg_stats(ws, args, ctx, owner):
    from . import code_graph as cg
    return cg.stats(_cg_db(ws), repo=_cg_repo(ws, args))


def _h_cg_rationales(ws, args, ctx, owner):
    from . import code_graph as cg
    repo = _cg_repo(ws, args)
    if args.get("symbol"):
        return {"symbol": args["symbol"],
                "rationales": cg.rationales_for_symbol(_cg_db(ws), args["symbol"], repo=repo)}
    return {"path": args.get("path"),
            "rationales": cg.rationales_in_file(_cg_db(ws), args.get("path", ""), repo=repo)}


def _h_cg_update_files(ws, args, ctx, owner):
    """Incrementally update the structural graph after editing files — INSTANT,
    no clone. files=[{path, text}] with FULL new content. Call right after
    writing code (NOT ingest_github)."""
    from . import code_graph as cg
    files = args.get("files") or []
    repo = _cg_repo(ws, args)
    if not files:
        return {"error": "files required: [{path, text}, ...]"}
    if not repo:
        return {"error": "no repo in graph yet — ingest first"}
    return cg.update_files(_cg_db(ws), files, repo)


def _h_cg_semantic_stale(ws, args, ctx, owner):
    from . import code_graph as cg
    rows = cg.list_semantic_stale(_cg_db(ws), repo=_cg_repo(ws, args))
    return {"stale_files": rows, "count": len(rows)}


def _h_cg_context_pack(ws, args, ctx, owner):
    """PHASE 2 / CAG: tiered blueprint slice (overview always + folder sections
    on-demand). Fast — no graph load, no LLM. Preload at session start."""
    import os, re as _re
    from .context_pack import get_context_pack
    bp_dir = os.path.join(ws.dg.storage_dir, "blueprints")
    repo = args.get("repo")
    bp = ""
    if os.path.isdir(bp_dir):
        if repo:
            safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", repo.replace("/", "_"))[:120]
            cand = os.path.join(bp_dir, f"{safe}.md")
            if os.path.exists(cand):
                bp = cand
        if not bp:
            mds = [f for f in os.listdir(bp_dir) if f.endswith(".md")]
            if len(mds) == 1:
                bp = os.path.join(bp_dir, mds[0])
    if not bp:
        avail = [f[:-3] for f in os.listdir(bp_dir)] if os.path.isdir(bp_dir) else []
        return {"error": "blueprint not found; pass repo=", "available": avail}
    pack = get_context_pack(bp, args.get("task_files") or [],
                            token_budget=int(args.get("token_budget") or 6000),
                            include_folder_siblings=bool(args.get("include_folder_siblings", True)))
    pack["blueprint"] = os.path.basename(bp)
    return pack


# ── AI-edit capture ─────────────────────────────────────────────────────────
def _build_unified_diff(before: str, after: str, file_path: str,
                          max_lines: int = 200) -> str:
    """Compute a unified diff (truncated) between before and after text.
    Returns a string suitable for storing as the 'answer' field of a decision."""
    import difflib
    bef_lines = (before or "").splitlines(keepends=False)
    aft_lines = (after or "").splitlines(keepends=False)
    diff = list(difflib.unified_diff(
        bef_lines, aft_lines,
        fromfile=f"a/{file_path}", tofile=f"b/{file_path}",
        lineterm="", n=3))
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [
            f"... [+{len(diff) - max_lines} more lines truncated]"]
    return "\n".join(diff)


def _h_track_code_edit(ws, args, ctx, owner):
    """Agent reports an edit it just made to a file. The platform records:
    who (agent token), when (now), where (file:line), what (unified diff),
    why (prompt + reasoning) — as one permanent decision in the worker's DG.
    Becomes queryable later by humans or future AI sessions to understand the
    intent behind any change.

    Args:
      file_path:  path edited (relative or absolute)
      before:     full prior text of the file (or relevant region)
      after:      full new text
      reasoning:  optional — the agent's own explanation of why
      prompt:     optional — the user's prompt that triggered the change
      line_range: optional — "L120-L160" if only a region changed
      repo:       optional — repo name for tagging
    """
    file_path = (args.get("file_path") or "").strip()
    if not file_path:
        return _err("file_path required")
    before = args.get("before") or ""
    after  = args.get("after")  or ""
    if not (before or after):
        return _err("must provide before and/or after text")
    reasoning  = (args.get("reasoning")  or "").strip()
    prompt     = (args.get("prompt")     or "").strip()
    line_range = (args.get("line_range") or "").strip()
    repo       = (args.get("repo")       or "").strip()

    diff = _build_unified_diff(before, after, file_path)
    # tag who did it: scoped agent name, or 'owner' if it was an owner token
    agent_tag = "owner" if owner else (
        (ctx or {}).get("agent_token", "")[:14] or "agent")
    when = datetime.now().isoformat(timespec="seconds")
    citation = f"code-edit://{file_path}?at={when}"
    if line_range:
        citation += f"&lines={line_range}"
    if repo:
        citation += f"&repo={repo}"

    # decision-store: the prompt is the question, the diff is the answer,
    # reasoning_summary captures the agent's "why"
    question = f"[edit:{agent_tag}] {file_path}"
    if line_range:
        question += f" @ {line_range}"
    if prompt:
        question += f" — \"{prompt[:120]}\""
    answer = diff or "(no diff — files identical)"

    reasoning_full = (
        f"AI edit by {agent_tag} at {when}; "
        f"file={file_path}; lines={line_range or '?'}; "
        f"reasoning={reasoning[:1000] or '(none provided)'}; "
        f"prompt={prompt[:500] or '(none provided)'}; "
        f"repo={repo or '?'}; citation={citation}")

    try:
        did = ws.dg.memory.store(
            question=question[:500],
            answer=answer[:6000],
            reasoning_summary=reasoning_full[:2500],
            communities_used=[], context_triples=[])
        try: ws.dg.memory.save()
        except Exception: pass
    except Exception as e:
        return _err(f"store failed: {e}")
    return {
        "decision_id":  did,
        "citation":     citation,
        "agent":        agent_tag,
        "timestamp":    when,
        "lines_in_diff": diff.count("\n") + 1 if diff else 0,
    }


def _h_track_decision(ws, args, ctx, owner):
    """Lightweight: the agent records an arbitrary decision (no diff needed).
    Use for non-file work: 'I decided to use Postgres because X', 'I rejected
    approach Y because Z'. Becomes a queryable decision node."""
    question = (args.get("question") or "").strip()
    answer   = (args.get("answer")   or "").strip()
    if not question or not answer:
        return _err("question and answer required")
    reasoning = (args.get("reasoning") or "").strip()
    topic     = (args.get("topic")     or "").strip()
    agent_tag = "owner" if owner else (
        (ctx or {}).get("agent_token", "")[:14] or "agent")
    when = datetime.now().isoformat(timespec="seconds")

    tag = f"[decision:{agent_tag}]"
    if topic:
        tag += f" {topic} ·"
    try:
        did = ws.dg.memory.store(
            question=f"{tag} {question}"[:500],
            answer=answer[:4000],
            reasoning_summary=(
                f"Logged by {agent_tag} at {when}; "
                f"reasoning={reasoning[:1500] or '(none)'}; topic={topic or '?'}")[:2500],
            communities_used=[], context_triples=[])
        try: ws.dg.memory.save()
        except Exception: pass
    except Exception as e:
        return _err(f"store failed: {e}")
    return {"decision_id": did, "agent": agent_tag, "timestamp": when}


def _h_recent_code_edits(ws, args, ctx, owner):
    """Return the most recent code-edit decisions captured in this workspace.
    For human review or audit. Optional filters: file_path prefix, limit."""
    limit  = max(1, min(int(args.get("limit") or 20), 200))
    prefix = (args.get("file_path") or "").strip()
    rows   = ws.dg.memory.all_decisions()
    edits  = []
    for r in rows:
        q = r.get("question") or ""
        if not q.startswith("[edit:"):
            continue
        if prefix and prefix not in q:
            continue
        edits.append({
            "decision_id": r.get("id"),
            "summary":     q[:200],
            "diff":        (r.get("answer") or "")[:2000],
            "context":     (r.get("reasoning_summary") or "")[:600],
            "timestamp":   r.get("timestamp"),
        })
    edits.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return {"edits": edits[:limit], "total_seen": len(edits)}


def _h_forecast(ws, args, ctx, owner):
    """Time-series forecast. Backends tried in order: TimesFM (if installed) →
    statsmodels Holt-Winters → numpy linear. Always works; quality matches
    the best available backend.

    args: { values: [float, ...], horizon: int=12,
            seasonal_periods?: int, prefer_backend?: str }
    """
    from .forecasting import forecast as _fc
    values = args.get("values") or []
    horizon = int(args.get("horizon") or 12)
    sp = args.get("seasonal_periods")
    prefer = args.get("prefer_backend")
    out = _fc(values, horizon=horizon,
               seasonal_periods=int(sp) if sp else None,
               prefer_backend=prefer)
    return out


def _h_forecast_backend_info(ws, args, ctx, owner):
    """Tell the caller which forecasting backend will be used."""
    from .forecasting import detect_backend
    return {"backend": detect_backend()}


def _h_job_status(ws, args, ctx, owner):
    jid = int(args.get("job_id") or 0)
    if not jid:
        return _err("job_id required")
    j = ctx["JOBS"].get(jid)
    return j or _err("job not found")


# ── 2. VIEW GRAPH ──────────────────────────────────────────────────────────
def _h_get_graph(ws, args, ctx, owner):
    """Scope-aware graph view. Mirrors the /api/graph endpoint exactly so
    MCP callers see the same four lenses the visualizer uses.

    scope:
      'knowledge'  → the knowledge graph triples (default)
      'decisions'  → decision nodes + edges between decisions that share a community
      'sessions'   → discussion sessions and the key decisions they emitted
      'companies'  → companies + their docs (overview), or one company's KG (with company_id)
    """
    max_n = int(args.get("max_nodes") or 200)
    scope = (args.get("scope") or "knowledge").lower()
    company_id = args.get("company_id")
    nodes, edges = [], []

    if scope == "knowledge":
        G = ws.dg.G
        if G is None:
            return {"scope": scope, "nodes": [], "edges": [], "communities": 0}
        node_list = list(G.nodes())[:max_n]
        nset = {str(n) for n in node_list}
        nodes = [{"id": str(n)} for n in node_list]
        for u, v, d in G.edges(data=True):
            if str(u) in nset and str(v) in nset:
                edges.append({"subject": str(u),
                              "relation": d.get("relation", ""),
                              "object": str(v)})
        return {"scope": scope, "nodes": nodes, "edges": edges[:1000],
                "communities": len(ws.dg.summaries or {})}

    if scope == "decisions":
        decs = ws.dg.memory.get_active_decisions()[:max_n]
        for d in decs:
            nodes.append({"id": d.get("id"),
                          "question": d.get("question", ""),
                          "answer": (d.get("answer") or "")[:200],
                          "confidence": d.get("confidence", 0),
                          "timestamp": d.get("timestamp", ""),
                          "communities_used": d.get("communities_used", [])})
        # edge: two decisions sharing a community
        for i, d1 in enumerate(decs):
            c1 = set(d1.get("communities_used", []))
            if not c1:
                continue
            for d2 in decs[i + 1:]:
                shared = c1 & set(d2.get("communities_used", []))
                if shared:
                    edges.append({"from": d1["id"], "to": d2["id"],
                                  "shared_communities": sorted(shared)[:5]})
        return {"scope": scope, "decisions": nodes, "edges": edges,
                "count": len(nodes)}

    if scope == "sessions":
        for ss in ws.dm.list_sessions()[:max_n]:
            nodes.append({"id": ss.get("id"),
                          "title": ss.get("title", ""),
                          "messages_count": ss.get("messages_count", 0),
                          "key_decisions": (ss.get("key_decisions") or [])[:5]})
        return {"scope": scope, "sessions": nodes, "count": len(nodes)}

    if scope == "companies":
        if company_id:
            cm = ws.hub.get_company(company_id)
            if not cm:
                return _err(f"company '{company_id}' not found")
            G = cm.company_dg.G
            if G is None:
                return {"scope": scope, "company_id": company_id,
                        "nodes": [], "edges": []}
            node_list = list(G.nodes())[:max_n]
            nset = {str(n) for n in node_list}
            nodes = [{"id": str(n)} for n in node_list]
            for u, v, d in G.edges(data=True):
                if str(u) in nset and str(v) in nset:
                    edges.append({"subject": str(u),
                                  "relation": d.get("relation", ""),
                                  "object": str(v)})
            return {"scope": scope, "company_id": company_id,
                    "nodes": nodes, "edges": edges[:1000]}
        # overview
        for cid, cm in (ws.hub.companies or {}).items():
            nodes.append({"id": cid, "name": getattr(cm, "company_name", cid),
                          "documents": len(getattr(cm, "registry", {}) or {})})
        return {"scope": scope, "companies": nodes, "count": len(nodes)}

    return _err(f"unknown scope '{scope}'. valid: knowledge|decisions|sessions|companies")


# ── 3. COMPANIES ───────────────────────────────────────────────────────────
def _h_list_companies(ws, args, ctx, owner):
    out = []
    for cid, cm in (ws.hub.companies or {}).items():
        out.append({"id": cid, "name": getattr(cm, "company_name", cid)})
    return {"companies": out}


def _h_create_company(ws, args, ctx, owner):
    name = (args.get("name") or "").strip()
    if not name:
        return _err("name required")
    cid = (args.get("company_id") or
           name.lower().replace(" ", "_").replace("-", "_")[:40])
    cm = ws.hub.add_company(cid, name)
    return {"id": cm.company_id, "name": cm.company_name}


# ── 4. ADD DOCS TO COMPANY ─────────────────────────────────────────────────
def _h_ingest_company_document(ws, args, ctx, owner):
    cid = args.get("company_id"); path = (args.get("path") or "").strip()
    if not cid or not path:
        return _err("company_id and path required")
    cm = ws.hub.get_company(cid)
    if not cm:
        return _err(f"company {cid} not found")
    if not os.path.isfile(path):
        return _err(f"file not found: {path}")
    cm.ingest(path)
    return {"ok": True, "stats": cm.stats()}


def _h_list_company_docs(ws, args, ctx, owner):
    cid = args.get("company_id")
    if not cid:
        return _err("company_id required")
    cm = ws.hub.get_company(cid)
    if not cm:
        return _err(f"company {cid} not found")
    return {"company_id": cid, "documents": cm.list_documents()
            if hasattr(cm, "list_documents") else []}


# ── 5. QUERY WITH MODES ────────────────────────────────────────────────────
def _h_query(ws, args, ctx, owner):
    q = (args.get("question") or "").strip()
    if not q:
        return _err("question required")
    mode = (args.get("mode") or "auto").lower()
    company_id = args.get("company_id")
    hybrid = bool(args.get("hybrid"))

    from decisiongraph.query import classify_intent, intent_to_mode
    if mode == "auto":
        intent = classify_intent(q)
        mode = intent_to_mode(intent)
    else:
        intent = mode

    from decisiongraph.agent import normal_mode, session_mode, react_agent
    target_dg = ws.dg
    company_meta = None
    if company_id:
        cm = ws.hub.get_company(company_id)
        if not cm:
            return _err(f"company {company_id} not found")
        target_dg = cm.company_dg
        company_meta = {"name": cm.company_name}

    if mode == "normal":
        ans = normal_mode(q, target_dg.client, target_dg.memory,
                          target_dg.embed_model)
    elif mode == "session":
        ans = session_mode(q, target_dg.client, G=target_dg.G,
                           community_summaries=target_dg.summaries,
                           community_ids=target_dg.community_ids,
                           community_embeddings=target_dg.community_embeddings,
                           embed_model=target_dg.embed_model,
                           memory=target_dg.memory,
                           company_metadata=company_meta)
    else:                                                       # deep
        ans = react_agent(q, target_dg.client, G=target_dg.G,
                          community_summaries=target_dg.summaries,
                          community_ids=target_dg.community_ids,
                          community_embeddings=target_dg.community_embeddings,
                          embed_model=target_dg.embed_model,
                          memory=target_dg.memory)
    return {"mode": mode, "intent": intent, "answer": ans,
            "company_id": company_id}


# ── 6. DISCUSSION MODE ─────────────────────────────────────────────────────
def _h_start_session(ws, args, ctx, owner):
    s = ws.dm.start_session(args.get("title") or "untitled")
    return {"session_id": s["id"] if isinstance(s, dict) else s}


def _h_send_message(ws, args, ctx, owner):
    sid = args.get("session_id"); txt = args.get("text") or ""
    if not sid:
        return _err("session_id required")
    r = ws.dm.add_message(sid, txt)
    return {"ok": True, "result": r}


def _h_end_session(ws, args, ctx, owner):
    sid = args.get("session_id")
    if not sid:
        return _err("session_id required")
    r = ws.dm.end_session(sid)
    return {"ok": True, "result": r}


def _h_list_sessions(ws, args, ctx, owner):
    return {"sessions": ws.dm.list_sessions()}


# ── 7. BRAIN TOOLS ─────────────────────────────────────────────────────────
def _h_get_compiled(ws, args, ctx, owner):
    cid = args.get("community_id")
    if cid is not None:
        try: cid = int(cid)
        except Exception: pass
        return ws.dg.memory.get_compiled(cid) or {}
    return {"compiled": ws.dg.memory.all_compiled()
            if hasattr(ws.dg.memory, "all_compiled") else {}}


def _h_get_timeline(ws, args, ctx, owner):
    topic = args.get("topic")
    if hasattr(ws.dg.memory, "get_timeline"):
        return {"timeline": ws.dg.memory.get_timeline(topic)}
    return {"timeline": []}


def _h_recall_entity(ws, args, ctx, owner):
    e = (args.get("entity") or "").strip().lower()
    if not e:
        return _err("entity required")
    hits = []
    for d in ws.dg.memory.get_active_decisions():
        if e in f"{d.get('question','')} {d.get('answer','')}".lower():
            hits.append({"id": d.get("id"),
                          "question": d.get("question", ""),
                          "answer": d.get("answer", ""),
                          "confidence": d.get("confidence", 0)})
    return {"entity": e, "hits": hits[:int(args.get("limit", 8))]}


def _h_run_dream_cycle(ws, args, ctx, owner):
    jid = ctx["JOBS"].enqueue(
        "dream", {"workspace": ws.token,
                  "compile_topics": bool(args.get("compile_topics", True)),
                  "dedupe": bool(args.get("dedupe", True))},
        workspace=ws.token)
    return {"job_id": jid, "queued": True}


def _h_export_workspace(ws, args, ctx, owner):
    # mirrors /api/workspace/export
    return {
        "decisions": ws.dg.memory.get_active_decisions(),
        "stats": ws.dg.stats(),
    }


def _h_mark_outcome(ws, args, ctx, owner):
    did = args.get("decision_id"); outcome = args.get("outcome")
    if not did or not outcome:
        return _err("decision_id and outcome required")
    ok = ws.dg.memory.set_outcome(did, outcome) if hasattr(
        ws.dg.memory, "set_outcome") else False
    ws.dg.memory.save()
    return {"ok": bool(ok), "decision_id": did, "outcome": outcome}


def _h_supersede(ws, args, ctx, owner):
    old = args.get("old_id"); new = args.get("new_id")
    if not old or not new:
        return _err("old_id and new_id required")
    ok = ws.dg.memory.supersede(old, new) if hasattr(
        ws.dg.memory, "supersede") else False
    ws.dg.memory.save()
    return {"ok": bool(ok)}


def _h_get_decision_chain(ws, args, ctx, owner):
    did = args.get("decision_id")
    if not did:
        return _err("decision_id required")
    if hasattr(ws.dg.memory, "get_chain"):
        return {"chain": ws.dg.memory.get_chain(did)}
    return {"chain": []}


def _h_decay(ws, args, ctx, owner):
    if hasattr(ws.dg.memory, "decay_confidence"):
        n = ws.dg.memory.decay_confidence()
        ws.dg.memory.save()
        return {"decayed": n}
    return _err("decay not supported")


# ── 8. SIMULATION ─────────────────────────────────────────────────────────
def _sim_to_dict(sim) -> dict:
    """Convert a SimulationState dataclass to a JSON-safe dict."""
    if sim is None:
        return None
    if isinstance(sim, dict):
        return sim
    try:
        from dataclasses import asdict
        return asdict(sim)
    except Exception:
        return {k: getattr(sim, k, None) for k in
                ("id", "decision_text", "personas", "depth", "company_id",
                 "mode", "status", "stage", "progress", "report", "error",
                 "started_at", "ended_at")}


def _h_start_simulation(ws, args, ctx, owner):
    """Start a scenario simulation. Args:
       scenario (str): the decision/scenario text
       personas (list[str]): persona names (optional; defaults to all)
       depth (int): rounds (default 10)
       company_id (str): optional company scope
    Mirofish is optional — if not configured, runs locally with the LLM gateway.
    """
    sim_mgr = ctx["S_simulator"]
    scenario = (args.get("scenario") or args.get("decision_text") or "").strip()
    if not scenario:
        return _err("scenario required")
    personas = args.get("personas") or []
    depth = int(args.get("depth") or 10)
    company_id = args.get("company_id") or ""
    company_context = ""
    if company_id:
        cm = ws.hub.get_company(company_id)
        if cm:
            company_context = f"Company: {cm.company_name}"
    settings = ctx.get("settings") or {}
    sid = sim_mgr.start(
        decision_text=scenario, personas=personas, depth=depth,
        company_context=company_context, company_id=company_id,
        client=ws.dg.client, embed_model=ws.dg.embed_model,
        mirofish_url=settings.get("mirofish_url", ""))
    return {"sim_id": sid, "queued": True,
            "note": "Simulation runs async. Poll get_simulation(sim_id) until status=='done'."}


def _h_get_simulation(ws, args, ctx, owner):
    sid = args.get("sim_id")
    if not sid:
        return _err("sim_id required")
    s = ctx["S_simulator"].get(sid)
    if s is None:
        return _err("sim not found")
    return _sim_to_dict(s)


def _h_list_simulations(ws, args, ctx, owner):
    sims = ctx["S_simulator"].list_all() if hasattr(
        ctx["S_simulator"], "list_all") else []
    return {"simulations": [_sim_to_dict(s) for s in sims]}


def _h_store_simulation(ws, args, ctx, owner):
    """Persist a finished simulation back into DecisionMemory."""
    sid = args.get("sim_id")
    if not sid:
        return _err("sim_id required")
    sim = ctx["S_simulator"].get(sid)
    if not sim:
        return _err("sim not found")
    if getattr(sim, "status", "") != "done":
        return _err(f"simulation not finished (status={getattr(sim,'status','?')})")
    r = getattr(sim, "report", None) or {}
    answer = (
        f"Consensus score: {r.get('consensus_score','?')}/100\n\n"
        f"Risks:\n" + "\n".join(f"- {x}" for x in r.get("risks", [])) + "\n\n"
        f"Opportunities:\n" + "\n".join(f"- {x}" for x in r.get("opportunities", [])) + "\n\n"
        f"Timeline:\n" + "\n".join(f"- {k}: {v}"
                                    for k, v in (r.get("timeline") or {}).items()))
    target_mem = ws.dg.memory
    if getattr(sim, "company_id", ""):
        cm = ws.hub.get_company(sim.company_id)
        if cm and getattr(cm, "company_dg", None):
            target_mem = cm.company_dg.memory
    did = target_mem.store(
        question=f"[SIMULATION] {sim.decision_text}",
        answer=answer,
        reasoning_summary=(f"Simulation across {len(sim.personas)} personas, "
                            f"{sim.depth} rounds"),
        communities_used=[], context_triples=[])
    target_mem.save()
    return {"ok": True, "decision_id": did, "sim_id": sid}


# ── Registry ──────────────────────────────────────────────────────────────
# schema = MCP tools/list inputSchema; needs_scope = require `topic` arg + gate;
# owner_only = available only to owner-mode connections.
TOOLS: dict[str, dict] = {
    # 1. ingest
    "ingest_pdf":              dict(handler=_h_ingest_pdf,
        owner_only=True, needs_scope=False,
        schema={"type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]},
        description="Ingest a PDF (server-local path) into the personal graph."),
    "ingest_url":              dict(handler=_h_ingest_url,
        owner_only=True, needs_scope=False,
        schema={"type": "object",
                "properties": {"url": {"type": "string"},
                               "company_id": {"type": "string"}},
                "required": ["url"]},
        description="Queue async ingest of a web URL. Returns a job_id; poll job_status."),
    "ingest_youtube":          dict(handler=_h_ingest_youtube,
        owner_only=True, needs_scope=False,
        schema={"type": "object",
                "properties": {"url": {"type": "string"},
                               "company_id": {"type": "string"}},
                "required": ["url"]},
        description="Queue async ingest of a YouTube transcript. Returns a job_id."),
    "ingest_media":            dict(handler=_h_ingest_media,
        owner_only=True, needs_scope=False,
        schema={"type": "object",
                "properties": {"path": {"type": "string"},
                               "company_id": {"type": "string"}},
                "required": ["path"]},
        description="Queue async transcription of an audio/video file."),
    "job_status":              dict(handler=_h_job_status,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {"job_id": {"type": "integer"}},
                "required": ["job_id"]},
        description="Poll a background job (ingest/dream)."),
    # ── structural code-graph (instant, no LLM) ──
    "update_files":            dict(handler=_h_cg_update_files,
        owner_only=True, needs_scope=False,
        schema={"type": "object", "properties": {
            "files": {"type": "array", "items": {"type": "object",
                "properties": {"path": {"type": "string"}, "text": {"type": "string"}}}},
            "repo": {"type": "string"}}, "required": ["files"]},
        description="Incrementally update the code graph after editing files — "
                    "INSTANT, no clone. files=[{path, text}]. Call right after "
                    "writing code (NOT ingest_github)."),
    "find_callers":            dict(handler=_h_cg_find_callers,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {
            "name": {"type": "string"}, "repo": {"type": "string"},
            "limit": {"type": "integer"}}, "required": ["name"]},
        description="Who calls a function/class? Structural, instant."),
    "blast_radius":            dict(handler=_h_cg_blast_radius,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {
            "path": {"type": "string"}, "repo": {"type": "string"},
            "max_depth": {"type": "integer"}}, "required": ["path"]},
        description="What files break if this file/symbol changes? Structural."),
    "triage_pr":               dict(handler=_h_cg_triage_pr,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "repo": {"type": "string"}, "max_hops": {"type": "integer"}},
            "required": ["changed_files"]},
        description="Risk-score a set of changed files (0-100) + merge hint."),
    "topology":                dict(handler=_h_cg_topology,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {
            "repo": {"type": "string"}, "top_god": {"type": "integer"},
            "top_surprise": {"type": "integer"}}},
        description="God-nodes, surprising connections, orphans, deep inheritance."),
    "code_graph_stats":        dict(handler=_h_cg_stats,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {"repo": {"type": "string"}}},
        description="Counts in the structural code graph."),
    "rationales":              dict(handler=_h_cg_rationales,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {
            "symbol": {"type": "string"}, "path": {"type": "string"},
            "repo": {"type": "string"}}},
        description="Surface # WHY:/# HACK:/# SAFETY: rationales near a symbol/file."),
    "semantic_stale":          dict(handler=_h_cg_semantic_stale,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {"repo": {"type": "string"}}},
        description="Files edited since last semantic pass (pending refresh)."),
    "get_context_pack":        dict(handler=_h_cg_context_pack,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {
            "task_files": {"type": "array", "items": {"type": "string"}},
            "repo": {"type": "string"},
            "token_budget": {"type": "integer"},
            "include_folder_siblings": {"type": "boolean"}}},
        description="PHASE 2/CAG: tiered blueprint slice — overview always + folder sections on-demand. Fast, no LLM. Preload at session start."),
    "post_message":            dict(handler=_h_post_message,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
                "required": ["text"]},
        description="Post a message to a specific peer (by role name) or 'all'. Active inside an orchestration dialog run only."),
    "read_messages":           dict(handler=_h_read_messages,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"since_ts": {"type": "number"}, "limit": {"type": "integer"}}},
        description="Read recent dialog messages addressed to you or broadcast in the current run."),
    "wait_for_message":        dict(handler=_h_wait_for_message,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"since_ts": {"type": "number"}, "timeout_seconds": {"type": "number"}}},
        description="Block until a new dialog message arrives or timeout (max 120s)."),
    "register_peer_tool":      dict(handler=_h_register_peer_tool,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"name": {"type": "string"},
                               "description": {"type": "string"},
                               "input_schema": {"type": "object"}},
                "required": ["name"]},
        description="Declare a tool YOU provide for peers in this orchestration run. Other agents will see it in list_peer_tools and can call it via call_peer_tool."),
    "list_peer_tools":         dict(handler=_h_list_peer_tools,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {}},
        description="List tools other agents in this run have exposed."),
    "call_peer_tool":          dict(handler=_h_call_peer_tool,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"provider": {"type": "string"},
                               "tool_name": {"type": "string"},
                               "args": {"type": "object"},
                               "timeout_seconds": {"type": "number"}},
                "required": ["provider", "tool_name"]},
        description="Call a tool exposed by a peer agent in this run. Returns its result via the message bus; same scope+audit guarantees."),
    "list_peers":              dict(handler=_h_list_peers,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {}},
        description="List other agents in the current orchestration run."),
    "ingest_github":           dict(handler=_h_ingest_github,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"repo_url":             {"type": "string"},
                               "branch":               {"type": "string"},
                               "include_code":         {"type": "boolean"},
                               "use_ast":              {"type": "boolean"},
                               "include_call_edges":   {"type": "boolean"},
                               "include_hierarchical": {"type": "boolean"},
                               "include_prs":          {"type": "boolean"},
                               "incremental":          {"type": "boolean"}},
                "required":   ["repo_url"]},
        description="Ingest a public github.com URL into the user's DG. Full 6-layer pipeline: docs+commits+manifests, tree-sitter AST chunks summarised per function/class, folder/repo rollups, call-graph edges, recent PRs, incremental hash cache. Call this BEFORE editing an unfamiliar repo so the agent has full context."),
    "ingest_repo":             dict(handler=_h_ingest_repo,
        owner_only=True, needs_scope=False,
        schema={"type": "object",
                "properties": {"path": {"type": "string"},
                               "include_commits": {"type": "boolean"},
                               "include_files": {"type": "boolean"}},
                "required": ["path"]},
        description="Ingest a local git repo (READMEs, ADRs, manifests, CODEOWNERS, module docstrings, recent commits) into the DG memory. Stores each artifact as a [repo:<name>] decision."),
    "get_codebase_context":    dict(handler=_h_get_codebase_context,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"file_path": {"type": "string"},
                               "intent": {"type": "string"},
                               "repo_name": {"type": "string"},
                               "limit": {"type": "integer"}}},
        description="Get the most relevant DG decisions for a file/intent the user is currently editing. Call this BEFORE answering coding questions so the answer is grounded in project history. Returns hits + a ready-to-paste system-prompt context block."),

    # ── AI-edit capture: store every code change AS a decision ──
    "track_code_edit":         dict(handler=_h_track_code_edit,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"file_path":  {"type": "string"},
                               "before":     {"type": "string"},
                               "after":      {"type": "string"},
                               "reasoning":  {"type": "string"},
                               "prompt":     {"type": "string"},
                               "line_range": {"type": "string"},
                               "repo":       {"type": "string"}},
                "required":   ["file_path"]},
        description="Record an edit you (the agent) just made to a file. Pass before+after text — the platform computes a unified diff and stores it as a permanent decision in the user's DG, tagged with WHO did it, WHEN, WHERE in the file, WHY (your reasoning + the user's prompt). Call this after every meaningful code change so future sessions can audit why each modification happened."),
    "track_decision":          dict(handler=_h_track_decision,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"question":  {"type": "string"},
                               "answer":    {"type": "string"},
                               "reasoning": {"type": "string"},
                               "topic":     {"type": "string"}},
                "required":   ["question", "answer"]},
        description="Record an arbitrary decision you made that did NOT involve a code edit (e.g. 'I decided to use Postgres because X'). Becomes a permanent decision node in the user's DG."),
    "recent_code_edits":       dict(handler=_h_recent_code_edits,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"limit":     {"type": "integer"},
                               "file_path": {"type": "string"}}},
        description="Return the N most recent AI-driven code edits captured in this workspace. Useful for audit, change history, and giving the next agent context about what's been touched lately."),
    "forecast":                dict(handler=_h_forecast,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"values": {"type": "array",
                                          "items": {"type": "number"}},
                               "horizon": {"type": "integer", "default": 12},
                               "seasonal_periods": {"type": "integer"},
                               "prefer_backend": {"type": "string",
                                                  "enum": ["timesfm","statsmodels","linear"]}},
                "required": ["values"]},
        description="Forecast a univariate time series. Returns point + 95% lower/upper, plus which backend was used (TimesFM if installed, else Holt-Winters, else linear). Pair with simulate/ask for quant+qualitative decision analysis."),
    "forecast_backend_info":   dict(handler=_h_forecast_backend_info,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {}},
        description="Which forecasting backend is active right now."),
    "run_python":              dict(handler=_h_run_python,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"code": {"type": "string"},
                               "timeout_seconds": {"type": "integer"}},
                "required": ["code"]},
        description="Execute Python in an ephemeral, network-less sub-sandbox. Returns stdout, stderr, exit code. Useful for calculations, data wrangling, formatting, ad-hoc logic."),

    # 2. graph
    "get_graph":               dict(handler=_h_get_graph,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"scope": {"type": "string"},
                               "max_nodes": {"type": "integer"}}},
        description="Return the knowledge graph (nodes + edges) for this workspace."),

    # 3. companies
    "list_companies":          dict(handler=_h_list_companies,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {}},
        description="List companies in this workspace."),
    "create_company":          dict(handler=_h_create_company,
        owner_only=True, needs_scope=False,
        schema={"type": "object", "properties": {"name": {"type": "string"}},
                "required": ["name"]},
        description="Create a new company."),

    # 4. company docs
    "ingest_company_document": dict(handler=_h_ingest_company_document,
        owner_only=True, needs_scope=False,
        schema={"type": "object",
                "properties": {"company_id": {"type": "string"},
                               "path": {"type": "string"}},
                "required": ["company_id", "path"]},
        description="Ingest a document into a specific company."),
    "list_company_docs":       dict(handler=_h_list_company_docs,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"company_id": {"type": "string"}},
                "required": ["company_id"]},
        description="List documents ingested into a company."),

    # 5. query
    "query":                   dict(handler=_h_query,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"question": {"type": "string"},
                               "mode": {"type": "string",
                                        "enum": ["auto", "normal", "session", "deep"]},
                               "company_id": {"type": "string"},
                               "hybrid": {"type": "boolean"}},
                "required": ["question"]},
        description="Query the DG. mode=auto routes by intent (normal/session/deep)."),

    # 6. sessions
    "start_session":           dict(handler=_h_start_session,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {"title": {"type": "string"}}},
        description="Start a discussion session."),
    "send_message":            dict(handler=_h_send_message,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"session_id": {"type": "string"},
                               "text": {"type": "string"}},
                "required": ["session_id", "text"]},
        description="Send a message into a discussion session."),
    "end_session":             dict(handler=_h_end_session,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"]},
        description="End a discussion session (folds it into graph memory)."),
    "list_sessions":           dict(handler=_h_list_sessions,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {}},
        description="List all discussion sessions in this workspace."),

    # 7. brain
    "get_compiled":            dict(handler=_h_get_compiled,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"community_id": {"type": ["string", "integer"]}}},
        description="Get Compiled Truth for a community or all communities."),
    "get_timeline":            dict(handler=_h_get_timeline,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {"topic": {"type": "string"}}},
        description="Get the immutable timeline for a topic."),
    "recall_entity":           dict(handler=_h_recall_entity,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"entity": {"type": "string"},
                               "limit": {"type": "integer"}},
                "required": ["entity"]},
        description="Recall everything we know about an entity."),
    "run_dream_cycle":         dict(handler=_h_run_dream_cycle,
        owner_only=True, needs_scope=False,
        schema={"type": "object", "properties": {}},
        description="Queue a Dream Cycle (decay/dedupe/relink/compile). Returns job_id."),
    "export_workspace":        dict(handler=_h_export_workspace,
        owner_only=True, needs_scope=False,
        schema={"type": "object", "properties": {}},
        description="Export this workspace's decisions and stats."),
    "mark_outcome":            dict(handler=_h_mark_outcome,
        owner_only=True, needs_scope=False,
        schema={"type": "object",
                "properties": {"decision_id": {"type": "string"},
                               "outcome": {"type": "string"}},
                "required": ["decision_id", "outcome"]},
        description="Mark the outcome of a past decision."),
    "supersede":               dict(handler=_h_supersede,
        owner_only=True, needs_scope=False,
        schema={"type": "object",
                "properties": {"old_id": {"type": "string"},
                               "new_id": {"type": "string"}},
                "required": ["old_id", "new_id"]},
        description="Mark one decision as superseded by another."),
    "get_decision_chain":      dict(handler=_h_get_decision_chain,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"decision_id": {"type": "string"}},
                "required": ["decision_id"]},
        description="Get the causal chain for a decision."),
    "decay":                   dict(handler=_h_decay,
        owner_only=True, needs_scope=False,
        schema={"type": "object", "properties": {}},
        description="Run confidence decay on aged decisions."),

    # 8. simulation
    "start_simulation":        dict(handler=_h_start_simulation,
        owner_only=False, needs_scope=False,
        schema={"type": "object",
                "properties": {"scenario": {"type": "string"}},
                "required": ["scenario"]},
        description="Start a scenario simulation. Returns sim_id."),
    "get_simulation":          dict(handler=_h_get_simulation,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {"sim_id": {"type": "string"}},
                "required": ["sim_id"]},
        description="Get a simulation's current state/result."),
    "list_simulations":        dict(handler=_h_list_simulations,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {}},
        description="List simulations in this workspace."),
    "store_simulation":        dict(handler=_h_store_simulation,
        owner_only=False, needs_scope=False,
        schema={"type": "object", "properties": {"sim_id": {"type": "string"}},
                "required": ["sim_id"]},
        description="Persist a simulation result into the graph."),
}


def list_tools(owner: bool) -> list[dict]:
    """Return MCP tools/list schema entries for the requester."""
    out = []
    for name, t in TOOLS.items():
        if t["owner_only"] and not owner:
            continue
        out.append({"name": name,
                    "description": t.get("description", ""),
                    "inputSchema": t["schema"]})
    return out


def call_tool(name: str, ws, args: dict, ctx: dict, owner: bool) -> dict:
    """Dispatch a tool call. Returns either a payload dict or {"error": ...}."""
    t = TOOLS.get(name)
    if not t:
        return _err(f"unknown tool: {name}")
    if t["owner_only"] and not owner:
        return _err(f"tool '{name}' requires owner access")
    try:
        return t["handler"](ws, args or {}, ctx, owner)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
