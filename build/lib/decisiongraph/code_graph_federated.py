"""Federated queries across multiple code_graph.db files.

Use case: a user has multiple repos in different workspaces / projects.
They want to ask cross-repo questions: "who calls `Logger.warn` across all
my projects?" or "show me god-nodes across the whole org."

Each individual db remains untouched. This module just unions results.
"""
from __future__ import annotations
import os, sqlite3
from pathlib import Path
from typing import Iterable, Optional

from . import code_graph as cg


def _resolve_dbs(roots: Iterable[str]) -> list[tuple[str, str]]:
    """Walk one or more directory roots looking for ANY SQLite db that has
    a `symbols` table (i.e. is a code_graph). Returns [(db_path, repo_label),
    ...] where repo_label is best-effort from the parent directory or
    filename."""
    out: list[tuple[str, str]] = []
    seen_paths: set[str] = set()

    def _is_code_graph(p: Path) -> bool:
        try:
            conn = sqlite3.connect(str(p))
            r = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='symbols'").fetchone()
            conn.close()
            return r is not None
        except Exception:
            return False

    def _label_for(p: Path) -> str:
        # workspace-style: .../<wsid>/personal/code_graph.db → wsid
        if p.name == "code_graph.db" and len(p.parents) >= 2:
            return p.parents[1].name
        # bench-style: .../bench_graphs/flask.db → flask
        return p.stem

    for root in roots:
        rp = Path(root)
        if rp.is_file() and rp.suffix == ".db":
            if _is_code_graph(rp) and str(rp) not in seen_paths:
                seen_paths.add(str(rp))
                out.append((str(rp), _label_for(rp)))
            continue
        if not rp.is_dir(): continue
        for f in rp.rglob("*.db"):
            if str(f) in seen_paths: continue
            if _is_code_graph(f):
                seen_paths.add(str(f))
                out.append((str(f), _label_for(f)))
    return out


def federated_find_callers(roots: Iterable[str], symbol_name: str,
                            limit_per_db: int = 20) -> dict:
    """Run find_callers across every code_graph.db rooted under `roots`."""
    dbs = _resolve_dbs(roots)
    results = {}
    total = 0
    for (db, label) in dbs:
        try:
            rows = cg.find_callers(db, symbol_name, limit=limit_per_db)
            if rows:
                results[label] = {"db": db, "callers": rows, "count": len(rows)}
                total += len(rows)
        except Exception as e:
            results[label] = {"db": db, "error": str(e)}
    return {"symbol": symbol_name, "dbs_searched": len(dbs),
            "total_callers": total, "by_workspace": results}


def federated_topology(roots: Iterable[str], top_god: int = 10) -> dict:
    """God-nodes and stats across all databases. Aggregates god-nodes by
    leaf name — symbols with the same name across multiple repos accumulate
    a combined caller count (useful for finding library-wide chokepoints)."""
    dbs = _resolve_dbs(roots)
    combined_gods: dict[str, dict] = {}
    per_db_stats = {}
    for (db, label) in dbs:
        try:
            st = cg.stats(db)
            per_db_stats[label] = st
            t  = cg.analyze_topology(db, top_god=top_god, top_surprise=0)
            for g in t["god_nodes"]:
                leaf = g["leaf"]
                entry = combined_gods.setdefault(leaf, {
                    "leaf": leaf, "total_callers": 0,
                    "per_workspace": {}, "paths": [],
                    # qualified names per repo so user can see if they're
                    # the same concept or just same leaf-name
                    "qualified_names_per_workspace": {}})
                entry["total_callers"] += g["caller_count"]
                entry["per_workspace"][label] = g["caller_count"]
                entry["qualified_names_per_workspace"][label] = g["qualified_name"]
                if g["path"] not in entry["paths"]:
                    entry["paths"].append(g["path"])
        except Exception as e:
            per_db_stats[label] = {"error": str(e)}
    # likely-related heuristic: same qualified-name suffix across repos
    for entry in combined_gods.values():
        qns = list(entry["qualified_names_per_workspace"].values())
        if len(qns) >= 2:
            suffixes = {qn.split(".")[-1] for qn in qns}
            entry["likely_same_concept"] = len(suffixes) == 1
    top_combined = sorted(combined_gods.values(),
                          key=lambda x: x["total_callers"], reverse=True)
    return {
        "dbs_searched": len(dbs),
        "per_workspace_stats": per_db_stats,
        "cross_repo_god_nodes": top_combined[:top_god],
    }


def federated_rationales_search(roots: Iterable[str], query: str,
                                  tag: Optional[str] = None,
                                  limit_per_db: int = 50) -> dict:
    """Search rationale comments across all dbs. e.g. find every `# HACK:`
    mentioning 'race condition' across the org's repos."""
    dbs = _resolve_dbs(roots)
    matches: list[dict] = []
    for (db, label) in dbs:
        try:
            conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
            where = "text LIKE ?"
            args: list = [f"%{query}%"]
            if tag:
                where += " AND tag = ?"
                args.append(tag.upper())
            rows = conn.execute(
                f"SELECT tag, text, path, line, repo FROM rationales "
                f"WHERE {where} LIMIT ?", args + [limit_per_db]).fetchall()
            for r in rows:
                d = dict(r); d["workspace"] = label
                matches.append(d)
            conn.close()
        except Exception:
            continue
    return {"query": query, "tag": tag,
            "dbs_searched": len(dbs), "matches": matches}
