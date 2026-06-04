"""Phase 2 (slice 2) — The network directory across companies.

This is the seed of the "LinkedIn for agents" layer: a directory that spans
all company workspaces. CRITICAL isolation rule: it exposes ONLY de-identified
aggregate counts — never agent names, tokens, audit content, or any company's
data. A company id is an opaque short hash of the workspace token, so the
directory cannot be used to read across tenants (mirrors the existing
cross-company anonymized-patterns design).

Pure stdlib, read-only over the workspaces base dir.
"""
from __future__ import annotations

import os
import hashlib

from .agent_access import AgentAccessStore
from .agent_reputation import compute_reputation


def _opaque_id(token: str) -> str:
    return "co_" + hashlib.sha256(token.encode()).hexdigest()[:10]


def network_directory(workspaces_base: str) -> dict:
    """Scan every workspace that has an agent store and return an anonymized
    network-wide summary. No names, no tokens, no audit content leaves a
    company boundary."""
    companies = []
    total_agents = 0
    total_jobs = 0
    total_denied = 0

    try:
        entries = sorted(os.listdir(workspaces_base))
    except FileNotFoundError:
        entries = []

    for ws_token in entries:
        ws_root = os.path.join(workspaces_base, ws_token)
        agent_dir = os.path.join(ws_root, "agent")
        if not os.path.isdir(agent_dir):
            continue
        try:
            rep = compute_reputation(AgentAccessStore(ws_root))
        except Exception:
            continue
        if not rep:
            continue

        n_agents = len(rep)
        jobs = sum(a["jobs_completed"] for a in rep)
        denied = sum(a["access_denied"] for a in rep)
        # only an anonymized standings histogram leaves the boundary
        hist: dict[str, int] = {}
        for a in rep:
            hist[a["standing"]] = hist.get(a["standing"], 0) + 1

        companies.append({
            "company": _opaque_id(ws_token),
            "agents": n_agents,
            "jobs_completed": jobs,
            "scope_denials": denied,
            "standings": hist,
        })
        total_agents += n_agents
        total_jobs += jobs
        total_denied += denied

    companies.sort(key=lambda c: (-c["jobs_completed"], c["company"]))
    return {
        "companies": companies,
        "totals": {
            "companies": len(companies),
            "agents": total_agents,
            "jobs_completed": total_jobs,
            "scope_denials": total_denied,
        },
    }
