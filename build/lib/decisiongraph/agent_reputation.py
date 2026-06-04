"""Phase 2 (slice 1) — Agent reputation derived from the immutable audit log.

Reputation is NOT stored anywhere new. It is *computed* from the append-only
agent_audit.jsonl that Step-1/Step-4 already write. This is the moat in code:
an agent's standing lives in the company's audit history, not in the agent —
off this network it has no reputation at all.

Pure stdlib, deterministic, read-only over the audit log. No new state.

Scoring (transparent, bounded 0..100):
  start at 50 (neutral, unproven)
  +12  per job_completed
  +3   per learning_written        (it contributed knowledge)
  -10  per job_failed
  -15  per access_denied           (tried to exceed its scope)
  clamped to [0, 100]

Also exposes the raw counts so a UI/judge can see *why*, not just a number.
"""
from __future__ import annotations

from .agent_access import AgentAccessStore

_W = {
    "job_completed": 12,
    "learning_written": 3,
    "job_failed": -10,
    "access_denied": -15,
}
_BASE = 50


def _label(score: int) -> str:
    if score >= 80:
        return "trusted"
    if score >= 60:
        return "established"
    if score >= 40:
        return "unproven"
    if score >= 20:
        return "flagged"
    return "untrusted"


def compute_reputation(store: AgentAccessStore) -> list[dict]:
    """Return one reputation record per agent_name seen in the audit log,
    sorted by score descending then name."""
    agents: dict[str, dict] = {}

    for rec in store.read_audit(limit=100000):
        name = rec.get("agent_name")
        if not name:
            continue
        a = agents.setdefault(name, {
            "agent_name": name,
            "jobs_completed": 0,
            "jobs_failed": 0,
            "access_denied": 0,
            "learnings_written": 0,
            "grants_minted": 0,
            "first_seen": rec.get("ts"),
            "last_seen": rec.get("ts"),
        })
        ts = rec.get("ts")
        if ts is not None:
            if a["first_seen"] is None or ts < a["first_seen"]:
                a["first_seen"] = ts
            if a["last_seen"] is None or ts > a["last_seen"]:
                a["last_seen"] = ts

        ev = rec.get("event")
        if ev == "job_completed":
            a["jobs_completed"] += 1
        elif ev == "job_failed":
            a["jobs_failed"] += 1
        elif ev == "access_denied":
            a["access_denied"] += 1
        elif ev == "learning_written":
            a["learnings_written"] += 1
        elif ev == "grant_minted":
            a["grants_minted"] += 1

    out = []
    for a in agents.values():
        raw = (_BASE
               + _W["job_completed"] * a["jobs_completed"]
               + _W["learning_written"] * a["learnings_written"]
               + _W["job_failed"] * a["jobs_failed"]
               + _W["access_denied"] * a["access_denied"])
        score = max(0, min(100, raw))
        a["score"] = score
        a["standing"] = _label(score)
        out.append(a)

    out.sort(key=lambda x: (-x["score"], x["agent_name"]))
    return out
