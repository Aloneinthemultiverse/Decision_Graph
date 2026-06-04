"""Per-workspace, run-scoped message bus for multi-agent dialog.

Used during orchestration runs of mode='dialog' so sandboxed agents can
send_message / read_messages / wait_for_message / list_peers — i.e. they
talk to each other through a host-mediated channel rather than over a
network (which their sandboxes don't have).

Thread-safe. Backed by an append-only JSONL file in the workspace's agent
dir for replay/audit. One singleton per workspace.
"""
from __future__ import annotations

import os
import json
import time
import threading


_BUS_CACHE: dict[str, "MessageBus"] = {}
_CACHE_LOCK = threading.Lock()


class MessageBus:
    def __init__(self, ws_root: str):
        self.ws_root = ws_root
        self._dir = os.path.join(ws_root, "agent")
        os.makedirs(self._dir, exist_ok=True)
        self._path = os.path.join(self._dir, "orchestration_msgs.jsonl")
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._msgs: list[dict] = []
        # restore prior messages (so /api/orchestration/runs/{id} can show history)
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._msgs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass

    # ── write ────────────────────────────────────────────────────────────
    # ── tool mesh (S5): per-run registry of agent-exposed tools ──────────
    def declare_tool(self, run_id: str, provider: str,
                     name: str, description: str,
                     input_schema: dict | None = None) -> dict:
        with self._lock:
            reg = getattr(self, "_tools", None)
            if reg is None:
                self._tools = reg = {}
            per_run = reg.setdefault(run_id, {})
            per_provider = per_run.setdefault(provider, {})
            decl = {"provider": provider, "name": name,
                    "description": description or "",
                    "input_schema": input_schema or {}}
            per_provider[name] = decl
            return decl

    def list_tools_for_run(self, run_id: str) -> list[dict]:
        with self._lock:
            reg = getattr(self, "_tools", None) or {}
            out = []
            for prov, by_name in (reg.get(run_id) or {}).items():
                for name, decl in by_name.items():
                    out.append(decl)
            return out

    def post(self, run_id: str, from_agent: str, to: str, text: str,
             kind: str = "msg") -> dict:
        with self._lock:
            mid = f"m{len(self._msgs) + 1:06d}"
            rec = {"id": mid, "run_id": run_id, "from": from_agent,
                   "to": to or "all", "text": text, "kind": kind,
                   "ts": time.time()}
            self._msgs.append(rec)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            self._cond.notify_all()
            return rec

    # ── read (filtered) ──────────────────────────────────────────────────
    def read(self, run_id: str, agent: str, since_ts: float = 0.0,
             limit: int = 100) -> list[dict]:
        with self._lock:
            out: list[dict] = []
            for m in self._msgs:
                if m.get("run_id") != run_id:
                    continue
                if m.get("ts", 0) <= since_ts:
                    continue
                if m.get("from") == agent:           # don't echo own messages
                    continue
                if m.get("to") not in (agent, "all"):
                    continue
                out.append(m)
                if len(out) >= limit:
                    break
            return out

    def read_all_for_run(self, run_id: str) -> list[dict]:
        with self._lock:
            return [m for m in self._msgs if m.get("run_id") == run_id]

    def peers(self, run_id: str) -> list[str]:
        with self._lock:
            return sorted({m["from"] for m in self._msgs
                           if m.get("run_id") == run_id})

    # ── wait (blocking with timeout) ─────────────────────────────────────
    def wait_for(self, run_id: str, agent: str, since_ts: float = 0.0,
                 timeout_s: float = 20.0) -> list[dict]:
        deadline = time.time() + max(0.5, min(float(timeout_s), 120.0))
        with self._cond:
            while True:
                msgs = self.read(run_id, agent, since_ts=since_ts, limit=20)
                if msgs:
                    return msgs
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cond.wait(timeout=min(0.5, remaining))


def get_bus(ws_root: str) -> MessageBus:
    """Singleton per workspace."""
    with _CACHE_LOCK:
        b = _BUS_CACHE.get(ws_root)
        if b is None:
            b = MessageBus(ws_root)
            _BUS_CACHE[ws_root] = b
        return b
