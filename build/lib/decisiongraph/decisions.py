import uuid
import pickle
import os
import numpy as np
import networkx as nx
from datetime import datetime
from dataclasses import dataclass, field
from typing import List
from . import config


# Field defaults — also used by the load-time migration so old pickles
# (which lack these new attributes) get sensible values.
_NODE_DEFAULTS = {
    # Task 4 — outcomes
    "outcome": "unknown",          # unknown | success | failure | partial
    "outcome_notes": "",
    "outcome_recorded_at": "",
    "outcome_impact": 0.0,         # -1.0 to 1.0
    # Task 1 — forgetting
    "access_count": 0,
    "last_accessed": "",
    "confidence": 1.0,             # 0.0 .. 1.0
    "superseded_by": "",
    "is_active": True,
}


@dataclass
class DecisionNode:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    question: str = ""
    answer: str = ""
    reasoning_summary: str = ""
    communities_used: List[int] = field(default_factory=list)
    context_triples: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    # Task 4 — outcome tracking
    outcome: str = "unknown"
    outcome_notes: str = ""
    outcome_recorded_at: str = ""
    outcome_impact: float = 0.0
    # Task 1 — forgetting / decay
    access_count: int = 0
    last_accessed: str = ""
    confidence: float = 1.0
    superseded_by: str = ""
    is_active: bool = True


class DecisionMemory:
    def __init__(self, storage_dir: str = None):
        self.graph = nx.DiGraph()
        self._last_decay_run = ""    # ISO date — used to gate auto-decay
        self._compiled: dict = {}    # gbrain #1: community_id -> compiled truth
        # per-instance dir so each workspace's decision memory is isolated
        self.storage_dir = storage_dir or config.STORAGE_DIR

    # ── migration shim (Task 1 + 4): backfill new attributes on legacy pickles ──
    def _migrate_node(self, node):
        for k, v in _NODE_DEFAULTS.items():
            if not hasattr(node, k):
                setattr(node, k, v)
        return node

    def _all_nodes(self):
        """Yields (id, DecisionNode) tuples, ensuring each node is migrated."""
        for nid in self.graph.nodes():
            data = self.graph.nodes[nid].get("data")
            if data is None: continue
            yield nid, self._migrate_node(data)

    def store(self, question: str, answer: str, reasoning_summary: str,
              communities_used: list, context_triples: list,
              caused_by: str = None,        # Task 3 — relationships
              depends_on: str = None,
              related_to: list = None) -> str:
        node = DecisionNode(
            question=question,
            answer=answer,
            reasoning_summary=reasoning_summary,
            communities_used=communities_used,
            context_triples=context_triples,
        )
        self.graph.add_node(node.id, data=node)

        # Task 3 — auto-link to other decisions
        if caused_by and self.graph.has_node(caused_by):
            self.graph.add_edge(caused_by, node.id, relation="caused")
        if depends_on and self.graph.has_node(depends_on):
            self.graph.add_edge(node.id, depends_on, relation="depends_on")
        if related_to:
            for rid in related_to:
                if self.graph.has_node(rid):
                    self.graph.add_edge(node.id, rid, relation="related_to")

        # gbrain #5 — self-wiring typed links (ZERO LLM): auto-link this
        # decision to existing ones sharing a community.
        self._autolink(node.id)

        from .logging_setup import get_logger
        _log = get_logger("decisions")
        _log.debug("decision stored",
                   extra={"event": "decision.stored",
                           "decision_id": node.id,
                           "reasoning": reasoning_summary[:80]})
        # Auto-persist so callers don't have to remember to .save(). Cheap
        # because pickle write is incremental and only on mutation. Was a real
        # bug: `store()` was in-memory only; ADRs vanished on process exit.
        try:
            self.save()
        except Exception:
            pass    # never let persistence failure block the store-return
        return node.id

    # ── gbrain #5: deterministic auto-linking (no LLM) ──────────────────────
    def _autolink(self, decision_id: str) -> int:
        if not self.graph.has_node(decision_id):
            return 0
        node = self.graph.nodes[decision_id].get("data")
        if node is None:
            return 0
        mine = set(getattr(node, "communities_used", []) or [])
        if not mine:
            return 0
        added = 0
        for nid, other in list(self._all_nodes()):
            if nid == decision_id:
                continue
            shared = mine & set(getattr(other, "communities_used", []) or [])
            if shared and not self.graph.has_edge(decision_id, nid) \
                      and not self.graph.has_edge(nid, decision_id):
                self.graph.add_edge(decision_id, nid,
                                    relation="shares_community",
                                    communities=sorted(shared))
                added += 1
        return added

    def relink_all(self) -> int:
        """Re-run auto-linking across every decision (used by the Dream Cycle)."""
        total = 0
        for nid, _ in list(self._all_nodes()):
            total += self._autolink(nid)
        return total

    # ── gbrain #1: Compiled Truth + Timeline ────────────────────────────────
    def get_timeline(self, community_id) -> list:
        """Immutable, dated evidence log: every decision touching this topic,
        oldest → newest. Never edited."""
        rows = []
        for nid, n in self._all_nodes():
            if community_id in (getattr(n, "communities_used", []) or []):
                rows.append({
                    "id": nid, "question": n.question, "answer": n.answer[:400],
                    "timestamp": n.timestamp, "outcome": n.outcome,
                    "confidence": n.confidence, "is_active": n.is_active,
                })
        return sorted(rows, key=lambda r: r["timestamp"])

    def compile_topic(self, community_id, client, topic_summary: str = "") -> str:
        """Rewrite the 'current best understanding' for a topic from its
        ACTIVE decisions. Stored on the graph; timeline stays immutable."""
        timeline = [r for r in self.get_timeline(community_id) if r["is_active"]]
        if not timeline:
            self._compiled[str(community_id)] = {
                "text": "(no active decisions on this topic yet)",
                "compiled_at": datetime.now().isoformat(), "n": 0,
            }
            return self._compiled[str(community_id)]["text"]
        evidence = "\n".join(
            f"- [{r['outcome']}, conf={r['confidence']:.2f}] {r['question']}: {r['answer'][:200]}"
            for r in timeline[-25:]
        )
        prompt = (
            f"You maintain the institutional 'current best understanding' for a topic.\n"
            f"TOPIC: {topic_summary or ('community ' + str(community_id))}\n\n"
            f"ACTIVE DECISION EVIDENCE (oldest→newest):\n{evidence}\n\n"
            f"Write the COMPILED TRUTH: 3-6 sentences stating what the org "
            f"currently believes/should do on this topic, reconciling the "
            f"evidence (weight higher-confidence and successful outcomes; note "
            f"any unresolved tension). Plain prose, no preamble."
        )
        try:
            resp = client.messages.create(model=config.LLM_MODEL, max_tokens=600,
                messages=[{"role": "user", "content": prompt}])
            text = next((b.text.strip() for b in resp.content
                         if b.type == "text" and b.text.strip()), "")
        except Exception as e:
            text = f"(compile failed: {e})"
        self._compiled[str(community_id)] = {
            "text": text, "compiled_at": datetime.now().isoformat(),
            "n": len(timeline),
        }
        return text

    def get_compiled(self, community_id) -> dict:
        c = self._compiled.get(str(community_id))
        return {
            "community": community_id,
            "compiled_truth": (c or {}).get("text", ""),
            "compiled_at": (c or {}).get("compiled_at", ""),
            "evidence_count": (c or {}).get("n", 0),
            "timeline": self.get_timeline(community_id),
        }

    def all_compiled(self) -> dict:
        return dict(self._compiled)

    # ────────────────────────────────────────────────────────────────────────
    # Task 1 — forgetting / decay
    # ────────────────────────────────────────────────────────────────────────
    def access_decision(self, decision_id: str) -> None:
        """Called every time a decision is retrieved by query() — bumps usage."""
        if not self.graph.has_node(decision_id): return
        node = self._migrate_node(self.graph.nodes[decision_id]["data"])
        node.access_count += 1
        node.last_accessed = datetime.now().isoformat()

    def decay_confidence(self, days_threshold: int = 90, decay_step: float = 0.1,
                          inactive_below: float = 0.2) -> int:
        """Loop all decisions. If `last_accessed` is older than `days_threshold`
        (or never accessed and timestamp is older), decrement confidence.
        Mark `is_active=False` once confidence falls below `inactive_below`.
        Returns the number of nodes that were updated."""
        from datetime import timedelta
        now = datetime.now()
        cutoff = now - timedelta(days=days_threshold)
        changed = 0
        for nid, node in self._all_nodes():
            ref = node.last_accessed or node.timestamp
            try:
                last = datetime.fromisoformat(ref) if ref else now
            except Exception:
                last = now
            if last < cutoff and node.is_active:
                node.confidence = max(0.0, round(node.confidence - decay_step, 3))
                if node.confidence < inactive_below:
                    node.is_active = False
                changed += 1
        self._last_decay_run = now.date().isoformat()
        if changed:
            print(f"  Decay pass: {changed} decisions decayed.")
        return changed

    def _maybe_run_decay(self):
        """Idempotent — only runs the decay pass once per calendar day."""
        today = datetime.now().date().isoformat()
        if self._last_decay_run != today:
            self.decay_confidence()

    def supersede(self, old_decision_id: str, new_decision_id: str) -> bool:
        """Mark `old` as superseded by `new`. Adds a graph edge new --[supersedes]--> old."""
        if not (self.graph.has_node(old_decision_id) and self.graph.has_node(new_decision_id)):
            return False
        old = self._migrate_node(self.graph.nodes[old_decision_id]["data"])
        old.superseded_by = new_decision_id
        old.is_active = False
        self.graph.add_edge(new_decision_id, old_decision_id, relation="supersedes")
        return True

    def get_active_decisions(self) -> list:
        """Return only currently-active decisions, ordered by confidence desc."""
        rows = []
        for nid, node in self._all_nodes():
            if not node.is_active: continue
            rows.append({
                "id": nid,
                "question": node.question,
                "answer": node.answer[:200],
                "confidence": node.confidence,
                "outcome": node.outcome,
                "access_count": node.access_count,
                "last_accessed": node.last_accessed,
                "timestamp": node.timestamp,
            })
        return sorted(rows, key=lambda r: r["confidence"], reverse=True)

    # ────────────────────────────────────────────────────────────────────────
    # Task 4 — outcome tracking
    # ────────────────────────────────────────────────────────────────────────
    def update_outcome(self, decision_id: str, outcome: str,
                        notes: str = "", impact: float = 0.0) -> bool:
        """Record the real-world outcome of a decision. Adjusts confidence:
        failure → -0.3, success → +0.1 (clamped to [0, 1])."""
        if not self.graph.has_node(decision_id): return False
        node = self._migrate_node(self.graph.nodes[decision_id]["data"])
        node.outcome = outcome
        node.outcome_notes = notes
        node.outcome_recorded_at = datetime.now().isoformat()
        node.outcome_impact = max(-1.0, min(1.0, float(impact)))
        if outcome == "failure":
            node.confidence = max(0.0, round(node.confidence - 0.3, 3))
        elif outcome == "success":
            node.confidence = min(1.0, round(node.confidence + 0.1, 3))
        if node.confidence < 0.2:
            node.is_active = False
        return True

    def get_outcome_patterns(self) -> dict:
        """Group decisions by community_used and report success rate per community."""
        from collections import defaultdict
        bucket = defaultdict(lambda: {"total": 0, "success": 0, "failure": 0, "partial": 0, "unknown": 0})
        for nid, node in self._all_nodes():
            for cid in node.communities_used or []:
                b = bucket[str(cid)]
                b["total"] += 1
                b[node.outcome] = b.get(node.outcome, 0) + 1
        out = {}
        for cid, b in bucket.items():
            total = b["total"] or 1
            out[cid] = {
                "total_decisions": b["total"],
                "success_rate": round(b["success"] / total, 3),
                "failure_rate": round(b["failure"] / total, 3),
                "partial_rate": round(b["partial"] / total, 3),
                "unknown_rate": round(b["unknown"] / total, 3),
            }
        return out

    # ────────────────────────────────────────────────────────────────────────
    # Task 3 — decision relationships
    # ────────────────────────────────────────────────────────────────────────
    def get_decision_chain(self, decision_id: str) -> dict:
        """Return the lineage of `decision_id` — who caused it, what it depends
        on, what came after, and what it's related to."""
        if not self.graph.has_node(decision_id):
            return {"error": f"decision {decision_id} not found"}

        def _label(nid: str):
            d = self.graph.nodes[nid].get("data")
            return {"id": nid, "question": getattr(d, "question", "")[:80]} if d else {"id": nid}

        caused_by  = [_label(u) for u, _, e in self.graph.in_edges(decision_id, data=True)  if e.get("relation") == "caused"]
        depends_on = [_label(v) for _, v, e in self.graph.out_edges(decision_id, data=True) if e.get("relation") == "depends_on"]
        led_to     = [_label(v) for _, v, e in self.graph.out_edges(decision_id, data=True) if e.get("relation") == "caused"]
        related_to = [_label(v) for _, v, e in self.graph.out_edges(decision_id, data=True) if e.get("relation") == "related_to"]
        supersedes = [_label(v) for _, v, e in self.graph.out_edges(decision_id, data=True) if e.get("relation") == "supersedes"]
        return {
            "id": decision_id,
            "caused_by":  caused_by,
            "depends_on": depends_on,
            "led_to":     led_to,
            "related_to": related_to,
            "supersedes": supersedes,
        }

    def query(self, question: str, embed_model, top_k: int = 3) -> list:
        if self.graph.number_of_nodes() == 0:
            return []

        # Task 1: lazily decay before searching (idempotent — one pass/day max)
        self._maybe_run_decay()

        # Task 1: only search active decisions
        active = [(nid, node) for nid, node in self._all_nodes() if node.is_active]
        if not active:
            return []

        q_embedding = embed_model.encode([question])[0]
        past_questions = [d.question for _, d in active]
        past_embeddings = embed_model.encode(past_questions)

        q_norm = np.linalg.norm(q_embedding) or 1.0
        norms = np.linalg.norm(past_embeddings, axis=1)
        sims = np.dot(past_embeddings, q_embedding) / (norms * q_norm + 1e-9)

        # Task 1 + 4 scoring: similarity * confidence * outcome_weight
        OUTCOME_W = {"success": 1.2, "failure": 0.8, "partial": 1.0, "unknown": 1.0}
        scored = []
        for i, (nid, node) in enumerate(active):
            sim = float(sims[i])
            score = sim * float(node.confidence) * OUTCOME_W.get(node.outcome, 1.0)
            scored.append((score, sim, nid, node))
        scored.sort(key=lambda r: r[0], reverse=True)

        results = []
        for score, sim, nid, node in scored[:top_k]:
            if sim <= config.DECISION_SIMILARITY_THRESHOLD:
                continue
            # Task 1: access bookkeeping
            self.access_decision(nid)
            results.append({
                "id":                 nid,
                "similarity":         round(sim, 3),
                "score":              round(score, 3),
                "confidence":         node.confidence,
                "outcome":            node.outcome,
                "access_count":       node.access_count,
                "question":           node.question,
                "answer":             node.answer[:300],
                "reasoning_summary":  node.reasoning_summary,
            })
            print(f"  Past match (sim={sim:.3f} conf={node.confidence:.2f} outcome={node.outcome} score={score:.3f}): {node.question[:60]}...")
        return results

    def save(self, path: str = None):
        path = path or os.path.join(self.storage_dir, "decision_graph.pkl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({"graph": self.graph, "compiled": self._compiled,
                         "last_decay": self._last_decay_run}, f)
        from .logging_setup import get_logger
        get_logger("decisions").debug(
            "decision graph saved",
            extra={"event": "decision.graph.saved",
                   "count": self.graph.number_of_nodes()})

    def load(self, path: str = None):
        path = path or os.path.join(self.storage_dir, "decision_graph.pkl")
        if os.path.exists(path):
            with open(path, "rb") as f:
                blob = pickle.load(f)
            # new format = dict; legacy format = a raw DiGraph
            if isinstance(blob, dict) and "graph" in blob:
                self.graph = blob["graph"]
                self._compiled = blob.get("compiled", {}) or {}
                self._last_decay_run = blob.get("last_decay", "") or ""
            else:
                self.graph = blob          # legacy raw-graph pickle
            # backfill new attrs on legacy nodes so old pickles upgrade cleanly
            migrated = 0
            for nid in self.graph.nodes():
                data = self.graph.nodes[nid].get("data")
                if data is None: continue
                added_any = False
                for k, v in _NODE_DEFAULTS.items():
                    if not hasattr(data, k):
                        setattr(data, k, v); added_any = True
                if added_any: migrated += 1
            if migrated:
                print(f"  Migrated {migrated} legacy decisions (added outcome/confidence fields).")
            from .logging_setup import get_logger
            get_logger("decisions").info(
                "decision graph loaded",
                extra={"event": "decision.graph.loaded",
                       "count": self.graph.number_of_nodes()})
        else:
            print("  No decision graph found. Starting fresh.")

    def count(self) -> int:
        return self.graph.number_of_nodes()

    def all_decisions(self) -> list:
        rows = []
        for nid, d in self._all_nodes():
            rows.append({
                "id":                 nid,
                "question":           d.question,
                "answer":             d.answer[:200],
                "reasoning_summary":  d.reasoning_summary,
                "timestamp":          d.timestamp,
                "outcome":            d.outcome,
                "outcome_notes":      d.outcome_notes,
                "outcome_impact":     d.outcome_impact,
                "confidence":         d.confidence,
                "access_count":       d.access_count,
                "last_accessed":      d.last_accessed,
                "is_active":          d.is_active,
                "superseded_by":      d.superseded_by,
            })
        return rows
