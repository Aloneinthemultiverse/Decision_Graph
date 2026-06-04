"""Phase 3 (no billing, no currency) — agent marketplace catalog.

A tiny, file-backed registry of agents available for hire. Each entry points
at a vetted, repo-bundled sandbox script. The API only accepts catalog ids —
arbitrary script paths from clients are NEVER honored.

stdlib-only, read-only.
"""
from __future__ import annotations

import os
import json

_DEFAULT_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "agentnet", "agent_catalog.json")


class CatalogError(Exception):
    pass


def load_catalog(path: str | None = None) -> list[dict]:
    """Return the list of agents in the catalog (annotated with absolute
    script path resolved against the repo root)."""
    p = path or _DEFAULT_FILE
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    repo = os.path.dirname(p)
    out = []
    for a in data.get("agents", []):
        rel = a.get("script", "")
        abspath = os.path.normpath(os.path.join(repo, rel))
        # belt + braces: don't let a malformed catalog escape the repo
        if not abspath.startswith(repo):
            continue
        a = dict(a)
        a["script_path"] = abspath
        a["available"] = os.path.isfile(abspath)
        out.append(a)
    return out


def get_agent(agent_id: str, path: str | None = None) -> dict:
    """Return one catalog entry by id; raises if not found or unavailable."""
    for a in load_catalog(path):
        if a.get("id") == agent_id:
            if not a.get("available"):
                raise CatalogError(f"agent script missing on disk: {agent_id}")
            return a
    raise CatalogError(f"unknown agent id: {agent_id}")
