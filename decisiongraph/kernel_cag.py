"""Phase 2 — CAG layer: KERNEL-CAG (hot session scratchpad).

Two-tier CAG, brain analogy:
  KERNEL-CAG  = working memory — THIS session's activity (hot, ephemeral)
  DG-CAG      = long-term memory — the blueprint (stable, persistent)

KERNEL-CAG holds, for one running session:
  - the loaded blueprint slice (from get_context_pack)
  - recent edits, decisions, tool results, notes the agent produced
It is fast (local JSON), ephemeral (lives only for the session), and is the
thing the agent reads/writes DURING work so the slow DG is never on the hot
path. At SESSION END the kernel digests it (-> consolidate into DG, Phase 4).

Physical home: <storage>/kernel_cag/<session_id>.json
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

_KIND = {"edit", "decision", "tool_result", "note"}


class KernelCAG:
    """A single session's hot scratchpad, backed by one JSON file."""

    def __init__(self, storage_dir: str, session_id: Optional[str] = None):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.dir = os.path.join(storage_dir, "kernel_cag")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, f"{self.session_id}.json")
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {
                "session_id": self.session_id,
                "created": time.time(),
                "ended": None,
                "context_slice": None,   # the loaded blueprint slice (DG-CAG -> here)
                "entries": [],           # chronological activity log
            }
            self._flush()

    # ── persistence ──────────────────────────────────────────────────────
    def _flush(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ── DG-CAG slice load (session start) ────────────────────────────────
    def load_context_slice(self, pack: dict) -> None:
        """Store the blueprint slice (output of get_context_pack) as the
        session's preloaded context. This is the DG-CAG -> KERNEL-CAG handoff."""
        self._data["context_slice"] = {
            "markdown": pack.get("markdown", ""),
            "included_files": pack.get("included_files", []),
            "approx_tokens": pack.get("approx_tokens", 0),
            "loaded_at": time.time(),
        }
        self._flush()

    def get_context_slice(self) -> Optional[dict]:
        return self._data.get("context_slice")

    # ── hot activity log (during work) ───────────────────────────────────
    def append(self, kind: str, payload: dict) -> dict:
        if kind not in _KIND:
            raise ValueError(f"kind must be one of {sorted(_KIND)}, got {kind!r}")
        entry = {"ts": time.time(), "kind": kind, "payload": payload}
        self._data["entries"].append(entry)
        self._flush()
        return entry

    def recent(self, n: int = 20, kind: Optional[str] = None) -> list[dict]:
        items = self._data["entries"]
        if kind:
            items = [e for e in items if e["kind"] == kind]
        return items[-n:]

    # ── session end -> digest for consolidation (Phase 4 consumes this) ──
    def digest(self) -> dict:
        """Summarize 'what mattered' this session: edited files, decisions,
        counts. Phase 4 turns this into update_files + store_decision calls."""
        entries = self._data["entries"]
        edits = [e["payload"] for e in entries if e["kind"] == "edit"]
        decisions = [e["payload"] for e in entries if e["kind"] == "decision"]
        edited_files = sorted({
            e.get("path") for e in edits if e.get("path")
        })
        return {
            "session_id": self.session_id,
            "edited_files": edited_files,
            "edit_count": len(edits),
            "decisions": decisions,
            "decision_count": len(decisions),
            "tool_result_count": sum(1 for e in entries if e["kind"] == "tool_result"),
            "note_count": sum(1 for e in entries if e["kind"] == "note"),
            "duration_s": round(time.time() - self._data["created"], 1),
        }

    def close(self) -> dict:
        """Mark the session ended and return its digest. The file is left on
        disk for the consolidation step; callers may delete it after."""
        self._data["ended"] = time.time()
        self._flush()
        return self.digest()

    def discard(self) -> None:
        """Delete the scratchpad file (ephemeral cleanup)."""
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
