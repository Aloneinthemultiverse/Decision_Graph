"""gbrain #4 — the Dream Cycle.

A consolidation pass that makes a workspace's brain "smarter while it sleeps".
Reuses pieces that already exist (decay, outcome patterns, auto-link, compiled
truth) plus a deterministic near-duplicate merge. Zero-LLM except the optional
compiled-truth rewrite. Safe to run repeatedly; idempotent-ish.
"""
from __future__ import annotations
import time
from datetime import datetime


def _dup_pairs(mem, embed_model, threshold: float = 0.93):
    """Find near-duplicate ACTIVE decisions by question similarity."""
    import numpy as np
    active = [(nid, n) for nid, n in mem._all_nodes() if n.is_active]
    if len(active) < 2 or embed_model is None:
        return []
    qs = [n.question for _, n in active]
    embs = embed_model.encode(qs)
    norms = np.linalg.norm(embs, axis=1) + 1e-9
    pairs = []
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            sim = float(np.dot(embs[i], embs[j]) / (norms[i] * norms[j]))
            if sim >= threshold:
                pairs.append((active[i], active[j], sim))
    return pairs


def run_dream_cycle(workspace, *, compile_topics: bool = True,
                    dedupe: bool = True) -> dict:
    """Run the full nightly maintenance on ONE workspace. Returns a report."""
    t0 = time.time()
    report = {"started": datetime.now().isoformat(), "phases": {}}
    dg = workspace.dg
    mem = dg.memory

    # Phase 1 — decay confidence (forgetting)
    try:
        changed = mem.decay_confidence()
        report["phases"]["decay"] = {"decayed": changed}
    except Exception as e:
        report["phases"]["decay"] = {"error": str(e)}

    # Phase 2 — deterministic near-duplicate consolidation
    if dedupe:
        merged = 0
        try:
            for (ida, na), (idb, nb), sim in _dup_pairs(mem, dg.embed_model):
                # supersede the lower-confidence / older one
                keep, drop = ((ida, na), (idb, nb))
                if (na.confidence, na.timestamp) < (nb.confidence, nb.timestamp):
                    keep, drop = (idb, nb), (ida, na)
                if mem.graph.has_node(drop[0]) and drop[1].is_active:
                    mem.supersede(drop[0], keep[0])
                    merged += 1
            report["phases"]["dedupe"] = {"merged": merged}
        except Exception as e:
            report["phases"]["dedupe"] = {"merged": merged, "error": str(e)}

    # Phase 3 — self-wiring re-link (zero LLM)
    try:
        links = mem.relink_all()
        report["phases"]["relink"] = {"edges_added": links}
    except Exception as e:
        report["phases"]["relink"] = {"error": str(e)}

    # Phase 4 — outcome pattern recompute
    try:
        report["phases"]["patterns"] = {"communities": len(mem.get_outcome_patterns())}
    except Exception as e:
        report["phases"]["patterns"] = {"error": str(e)}

    # Phase 5 — recompile topic truths (optional, LLM)
    if compile_topics:
        compiled = 0
        try:
            summaries = dg.summaries or {}
            comms = set()
            for _, n in mem._all_nodes():
                for c in (n.communities_used or []):
                    comms.add(c)
            for cid in list(comms)[:30]:   # cap LLM calls per cycle
                summ = (summaries.get(cid, {}) or {}).get("summary", "")
                mem.compile_topic(cid, dg.client, topic_summary=summ)
                compiled += 1
            report["phases"]["compile"] = {"topics": compiled}
        except Exception as e:
            report["phases"]["compile"] = {"topics": compiled, "error": str(e)}

    # persist everything
    try:
        mem.save()
        report["phases"]["save"] = {"ok": True}
    except Exception as e:
        report["phases"]["save"] = {"error": str(e)}

    report["duration_s"] = round(time.time() - t0, 2)
    report["finished"] = datetime.now().isoformat()
    return report
