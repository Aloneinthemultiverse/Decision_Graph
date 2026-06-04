"""LOCAL-folder counterpart of ingest_github_url_v2.

This is a *duplicate* of `codebase.ingest_github_url_v2` with one change: it
skips the `git clone` step and uses the local folder as-is. Everything else
(blueprint build, code_graph SQLite, ws_dg.ingest pipeline) is identical, so
local OS-built sites land in the SAME knowledge graph that GitHub repos do.

The original `ingest_github_url_v2` is untouched.
"""
from __future__ import annotations
import os, re
from typing import Optional


def ingest_local_path_v2(ws_dg, local_path: str, repo_label: str,
                         progress_cb=None) -> dict:
    """Duplicate of `codebase.ingest_github_url_v2` but for a LOCAL folder.
    No clone step — point straight at `local_path` (e.g. the site the OS just built).
    `repo_label` is what the repo is called in the graph (e.g. 'os-build/coffee')."""
    if not os.path.isdir(local_path):
        return {"error": f"not a directory: {local_path}"}

    # parse "owner/repo" out of repo_label
    if "/" in repo_label:
        owner, repo = repo_label.split("/", 1)
    else:
        owner, repo = "os-build", repo_label

    # import lazily so importing this module never triggers heavy DG init
    from .codebase import _build_repo_blueprint

    try:
        # 1) blueprint markdown (same fn the github path uses)
        blueprint = _build_repo_blueprint(
            ws_dg, local_path, owner, repo, progress_cb=progress_cb)
        if not blueprint or blueprint.count("\n") < 5:
            return {"error": "blueprint generation produced empty output"}

        # persist blueprint into workspace storage
        bp_dir = os.path.join(ws_dg.storage_dir, "blueprints")
        os.makedirs(bp_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{owner}_{repo}")[:120]
        bp_path = os.path.join(bp_dir, f"{safe}.md")
        with open(bp_path, "w", encoding="utf-8") as f:
            f.write(blueprint)

        # 2) SQLite structural code graph (tree-sitter only, no LLM)
        try:
            from .code_graph import build_from_repo as _build_cg
            cg_db = os.path.join(ws_dg.storage_dir, "code_graph.db")
            cg_stats = _build_cg(cg_db, local_path, f"{owner}/{repo}",
                                   progress_cb=progress_cb)
        except Exception as e:
            cg_stats = {"error": str(e)}

        # 3) feed blueprint through DG's ingest pipeline → triples + graph
        if progress_cb: progress_cb("ingesting blueprint into DG", 0.96)
        try:
            ws_dg.ingest(bp_path)
        except Exception as e:
            # the deep ingest may crash on tiny inputs (AxisError). Don't lose
            # the work — the blueprint + code_graph are already on disk.
            return {
                "ok": False, "ingest_error": f"{type(e).__name__}: {e}",
                "blueprint_path": bp_path, "blueprint_chars": len(blueprint),
                "code_graph": cg_stats,
            }
        try: ws_dg.memory.save()
        except Exception: pass

        if progress_cb: progress_cb("done", 1.0)
        return {
            "ok":               True,
            "repo":             f"{owner}/{repo}",
            "owner":            owner,
            "local_path":       local_path,
            "blueprint_path":   bp_path,
            "blueprint_chars":  len(blueprint),
            "nodes":            ws_dg.G.number_of_nodes() if ws_dg.G is not None else 0,
            "edges":            ws_dg.G.number_of_edges() if ws_dg.G is not None else 0,
            "communities":      len(ws_dg.communities or {}),
            "code_graph":       cg_stats,
        }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
