"""Phase 4 — Consolidation: the "sleep" step.

Brain analogy (closing the loop started in Phase 2):
  KERNEL-CAG = working memory  — many short, hot session scratchpads on disk.
  DG long-term = the graph     — stable, deduped, decayed institutional memory.

During the day the kernel runs tasks; each `run_task` leaves an ENDED
KERNEL-CAG scratchpad (`<storage>/kernel_cag/<sid>.json`) on disk. Consolidation
is the periodic pass — run it like sleep — that:

  1. SCAN       collect every ended, not-yet-consolidated scratchpad.
  2. AGGREGATE  fold their digests into one cross-session view: which files
                churned (hot files), every decision, totals.
  3. WRITE-BACK record any decisions that weren't already pushed to DG, plus a
                single consolidation summary decision (the "what did we learn
                while sleeping" note).
  4. CLEAN      run the dream cycle (decay → dedupe → relink) so long-term
                memory gets *smarter*, then archive the consumed scratchpads
                out of the hot dir (move to kernel_cag/_archive/).

Idempotent: only processes scratchpads with `ended` set that still live in the
hot dir. Once archived they are never reprocessed. Zero-LLM on the hot path
(the dream cycle's optional topic-compile is the only LLM, and it's guarded).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter
from typing import Optional


_ARCHIVE = "_archive"


def _hot_dir(storage_dir: str) -> str:
    return os.path.join(storage_dir, "kernel_cag")


def _scan_ended(storage_dir: str) -> list[dict]:
    """Return parsed scratchpads that are ENDED and still in the hot dir."""
    d = _hot_dir(storage_dir)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(d, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if data.get("ended") is None:
            continue                      # still running — leave it alone
        data["_path"] = path
        out.append(data)
    return out


def _digest_of(data: dict) -> dict:
    entries = data.get("entries", [])
    edits = [e["payload"] for e in entries if e.get("kind") == "edit"]
    decisions = [e["payload"] for e in entries if e.get("kind") == "decision"]
    return {
        "session_id": data.get("session_id"),
        "edited_files": sorted({e.get("path") for e in edits if e.get("path")}),
        "edit_count": len(edits),
        "decisions": decisions,
        "decision_count": len(decisions),
    }


def consolidate(dg, storage_dir: str, *, run_dream: bool = True,
                workspace=None, archive: bool = True,
                write_summary: bool = True) -> dict:
    """Run one consolidation pass. `dg` is a DecisionGraph (needs .memory).
    `workspace` (optional) enables the full dream cycle; if absent we fall back
    to dg.memory's own decay/relink. Returns a report."""
    t0 = time.time()
    sessions = _scan_ended(storage_dir)
    report: dict = {
        "sessions_consolidated": 0,
        "session_ids": [],
        "total_edits": 0,
        "total_decisions": 0,
        "hot_files": [],
        "dream": None,
        "archived": 0,
        "summary_decision": None,
    }
    if not sessions:
        report["note"] = "no ended sessions to consolidate"
        report["duration_s"] = round(time.time() - t0, 2)
        return report

    # AGGREGATE
    churn: Counter = Counter()
    all_decisions: list[dict] = []
    for data in sessions:
        dig = _digest_of(data)
        report["session_ids"].append(dig["session_id"])
        report["total_edits"] += dig["edit_count"]
        report["total_decisions"] += dig["decision_count"]
        for f in dig["edited_files"]:
            churn[f] += 1
        all_decisions.extend(dig["decisions"])
    report["sessions_consolidated"] = len(sessions)
    report["hot_files"] = [{"path": p, "sessions": c} for p, c in churn.most_common(15)]

    # WRITE-BACK — a single cross-session summary decision (the "sleep" note).
    if write_summary and (report["total_edits"] or report["total_decisions"]):
        try:
            hot = ", ".join(f"{h['path']} (x{h['sessions']})" for h in report["hot_files"][:5])
            q = f"Consolidation across {len(sessions)} session(s)"
            a = (f"{report['total_edits']} edits, {report['total_decisions']} decisions. "
                 f"Hot files: {hot or 'none'}.")
            did = dg.memory.store(
                question=q, answer=a,
                reasoning_summary="Phase 4 sleep/consolidation pass over KERNEL-CAG scratchpads.",
                communities_used=[], context_triples=[])
            report["summary_decision"] = did
        except Exception as e:
            report["summary_decision"] = {"error": str(e)}

    # CLEAN — dream cycle (decay/dedupe/relink) makes long-term memory smarter.
    if run_dream:
        try:
            if workspace is not None:
                from .dream import run_dream_cycle
                report["dream"] = run_dream_cycle(workspace, compile_topics=False)
            else:
                # no full workspace: best-effort decay + relink directly on mem
                mem = dg.memory
                d = {}
                try:
                    d["decayed"] = mem.decay_confidence()
                except Exception:
                    pass
                try:
                    d["edges_added"] = mem.relink_all()
                except Exception:
                    pass
                mem.save()
                report["dream"] = d or {"note": "no-op"}
        except Exception as e:
            report["dream"] = {"error": str(e)}

    try:
        dg.memory.save()
    except Exception:
        pass

    # ARCHIVE — move consumed scratchpads out of the hot dir (idempotent).
    if archive:
        arc = os.path.join(_hot_dir(storage_dir), _ARCHIVE)
        os.makedirs(arc, exist_ok=True)
        n = 0
        for data in sessions:
            src = data.get("_path")
            if not src or not os.path.exists(src):
                continue
            try:
                shutil.move(src, os.path.join(arc, os.path.basename(src)))
                n += 1
            except Exception:
                pass
        report["archived"] = n

    report["duration_s"] = round(time.time() - t0, 2)
    return report
