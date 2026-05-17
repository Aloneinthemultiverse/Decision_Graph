import os
import pickle
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from . import config
from .core import DecisionGraph


# ─────────────────────────────────────────
# DOCUMENT CATEGORIES
# ─────────────────────────────────────────
DOCUMENT_CATEGORIES = {
    "DOCUMENTS": [
        "policy", "sop", "handbook", "documentation", "contract",
        "compliance", "guideline", "procedure", "manual", "standard"
    ],
    "DECISIONS": [
        "board", "meeting minutes", "strategy", "hiring", "vendor",
        "approval", "decision", "resolution", "agreement"
    ],
    "FINANCIAL": [
        "quarterly", "annual report", "budget", "p&l", "profit",
        "loss", "audit", "financial", "revenue", "expense", "forecast"
    ],
    "OPERATIONS": [
        "postmortem", "incident", "client", "support", "ticket",
        "operation", "process", "workflow", "project", "retrospective"
    ],
    "PEOPLE": [
        "employee", "performance", "exit interview", "team",
        "hr", "people", "talent", "culture", "onboarding"
    ],
    "MEETINGS": [
        "meeting notes", "action items", "discussion", "follow-up",
        "minutes", "agenda", "standup", "sync", "call"
    ]
}


def detect_category(filename: str, content_preview: str = "") -> str:
    text = (filename + " " + content_preview).lower()
    scores = {}
    for category, keywords in DOCUMENT_CATEGORIES.items():
        score = sum(1 for kw in keywords if kw in text)
        scores[category] = score
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "DOCUMENTS"


# ─────────────────────────────────────────
# COMPANY MEMORY
# ─────────────────────────────────────────
class CompanyMemory:
    """Per-company memory with TWO isolated knowledge graphs:
      - knowledge_dg : research papers, books, technical docs
      - company_dg   : meeting notes, financials, policies, internal docs

    Both are queried together via multi_graph_beam_query so the LLM sees facts
    from both sources, tagged with their origin.
    """
    # default root used only when no explicit dir is passed (CLI/standalone).
    _ROOT_STORAGE_DIR = config.STORAGE_DIR

    def __init__(self, company_id: str, company_name: str,
                 root_storage_dir: str = None, embed_model=None):
        self.company_id = company_id
        self.company_name = company_name
        root = root_storage_dir or CompanyMemory._ROOT_STORAGE_DIR
        self.storage_dir = os.path.join(root, "companies", company_id)
        # sub-dirs so each graph has fully isolated state
        self.knowledge_dir  = os.path.join(self.storage_dir, "knowledge")
        self.company_subdir = os.path.join(self.storage_dir, "company")
        os.makedirs(self.storage_dir,  exist_ok=True)
        os.makedirs(self.knowledge_dir, exist_ok=True)
        os.makedirs(self.company_subdir, exist_ok=True)

        # Construct each graph with its dir baked in — NO global config
        # mutation, so this is concurrency-safe across workspaces/threads.
        self.knowledge_dg = DecisionGraph(storage_dir=self.knowledge_dir, embed_model=embed_model)
        self.company_dg   = DecisionGraph(storage_dir=self.company_subdir, embed_model=embed_model)

        # backward-compat alias — most existing code expects cm.dg
        self.dg = self.company_dg

        # document registry
        self.registry_path = os.path.join(self.storage_dir, "registry.pkl")
        self.registry = self._load_registry()

        print(f"CompanyMemory initialized: {company_name} ({company_id}) "
              f"-> {self.storage_dir} (knowledge+company graphs)")

    @contextmanager
    def _scoped_storage(self, target_dir: str = None):
        """No-op now. Each DecisionGraph carries its own storage_dir, so there
        is no global state to patch. Kept so existing `with` call-sites stay
        valid without risky re-indentation."""
        yield

    def _load_registry(self) -> dict:
        if os.path.exists(self.registry_path):
            with open(self.registry_path, "rb") as f:
                return pickle.load(f)
        return {}

    def _save_registry(self):
        with open(self.registry_path, "wb") as f:
            pickle.dump(self.registry, f)

    def _preview(self, file_path: str) -> str:
        try:
            if file_path.endswith(".txt") or file_path.endswith(".md"):
                with open(file_path, "r", errors="ignore") as f:
                    return f.read(500)
        except Exception:
            pass
        return ""

    def _register(self, file_path: str, category: str, target_graph: str, metadata: dict = None) -> str:
        filename = os.path.basename(file_path)
        doc_id = f"{category}_{filename}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self.registry[doc_id] = {
            "filename":   filename,
            "category":   category,
            "target":     target_graph,        # "knowledge" or "company"
            "ingested_at": datetime.now().isoformat(),
            "metadata":   metadata or {},
        }
        self._save_registry()
        return doc_id

    def ingest_knowledge(self, file_path: str, metadata: dict = None) -> str:
        """Ingest a research/knowledge document (papers, books, technical docs)
        into the KNOWLEDGE graph."""
        filename = os.path.basename(file_path)
        print(f"\n{'='*50}\nCompany: {self.company_name}\nDocument: {filename}\n"
              f"Target: KNOWLEDGE graph\n{'='*50}")
        with self._scoped_storage(self.knowledge_dir):
            self.knowledge_dg.ingest(file_path)
        doc_id = self._register(file_path, "KNOWLEDGE", "knowledge", metadata)
        print(f"[OK] Registered into knowledge graph: {filename}")
        return doc_id

    def ingest_company(self, file_path: str, category: str = None, metadata: dict = None) -> str:
        """Ingest a company-meta document (meeting notes, financials, policies)
        into the COMPANY graph. Auto-detects category via detect_category()."""
        filename = os.path.basename(file_path)
        if not category:
            category = detect_category(filename, self._preview(file_path))
        print(f"\n{'='*50}\nCompany: {self.company_name}\nDocument: {filename}\n"
              f"Category: {category}\nTarget: COMPANY graph\n{'='*50}")
        with self._scoped_storage(self.company_subdir):
            self.company_dg.ingest(file_path)
        doc_id = self._register(file_path, category, "company", metadata)
        print(f"[OK] Registered as {category} into company graph: {filename}")
        return doc_id

    def ingest(self, file_path: str, category: str = None, metadata: dict = None) -> str:
        """Backward-compatible single ingest. Defaults to the COMPANY graph
        (matches original behaviour where 'ingest' meant the company side)."""
        return self.ingest_company(file_path, category=category, metadata=metadata)

    def _graphs_payload(self) -> list:
        """Build the `graphs` list expected by multi_graph_beam_query."""
        out = []
        if self.knowledge_dg.G is not None and self.knowledge_dg.G.number_of_nodes() > 0:
            out.append({
                "name": "Knowledge Graph",
                "G": self.knowledge_dg.G,
                "community_summaries": self.knowledge_dg.summaries or {},
                "community_ids": self.knowledge_dg.community_ids or [],
                "community_embeddings": self.knowledge_dg.community_embeddings,
            })
        if self.company_dg.G is not None and self.company_dg.G.number_of_nodes() > 0:
            out.append({
                "name": "Company Memory",
                "G": self.company_dg.G,
                "community_summaries": self.company_dg.summaries or {},
                "community_ids": self.company_dg.community_ids or [],
                "community_embeddings": self.company_dg.community_embeddings,
            })
        return out

    def query(self, question: str, mode: str = "session", hub=None) -> str:
        company_metadata = {
            "name": self.company_name,
            "documents_ingested": len(self.registry),
            "categories": {
                doc["category"]: sum(
                    1 for d in self.registry.values()
                    if d["category"] == doc["category"]
                )
                for doc in self.registry.values()
            }
        }

        # Task 5 — cross-company wisdom (only when an EnterpriseHub is passed in)
        if hub is not None:
            try:
                wisdom = hub.get_wisdom(question, company_id=self.company_id,
                                         embed_model=self.company_dg.embed_model)
                if wisdom:
                    company_metadata["wisdom"] = wisdom
            except Exception as e:
                print(f"  Wisdom lookup failed: {e}")

        graphs = self._graphs_payload()
        if not graphs:
            return "No documents ingested yet (neither knowledge nor company graph has data)."

        from .agent import session_mode, normal_mode, react_agent
        from . import config as cfg

        # decision memory is shared at the COMPANY level — scope storage to
        # company_subdir so memory.save() writes there
        with self._scoped_storage(self.company_subdir):
            if mode == cfg.QUERY_MODE_NORMAL:
                return normal_mode(
                    question=question,
                    client=self.company_dg.client,
                    memory=self.company_dg.memory,
                    embed_model=self.company_dg.embed_model,
                )

            elif mode == cfg.QUERY_MODE_SESSION:
                return session_mode(
                    question=question,
                    client=self.company_dg.client,
                    embed_model=self.company_dg.embed_model,
                    memory=self.company_dg.memory,
                    company_metadata=company_metadata,
                    graphs=graphs,             # ← multi-graph beam
                )

            else:  # deep mode (ReAct)
                return react_agent(
                    question=question,
                    client=self.company_dg.client,
                    embed_model=self.company_dg.embed_model,
                    memory=self.company_dg.memory,
                    graphs=graphs,             # ← multi-graph beam during ReAct
                )

    def stats(self) -> dict:
        category_counts = {}
        for doc in self.registry.values():
            cat = doc["category"]
            category_counts[cat] = category_counts.get(cat, 0) + 1

        return {
            "company": self.company_name,
            "company_id": self.company_id,
            "documents_ingested": len(self.registry),
            "categories": category_counts,
            "graph":           self.company_dg.stats(),    # backward compat alias
            "company_graph":   self.company_dg.stats(),
            "knowledge_graph": self.knowledge_dg.stats(),
        }

    def list_documents(self, category: str = None) -> list:
        docs = []
        for doc_id, doc in self.registry.items():
            if category and doc["category"] != category:
                continue
            docs.append({
                "id": doc_id,
                "filename": doc["filename"],
                "category": doc["category"],
                "ingested_at": doc["ingested_at"]
            })
        return sorted(docs, key=lambda x: x["ingested_at"], reverse=True)

    def get_decisions(self) -> list:
        return self.dg.get_decisions()


# ─────────────────────────────────────────
# COMPANY REGISTRY (multi-tenant)
# ─────────────────────────────────────────
class EnterpriseHub:
    def __init__(self, storage_dir: str = None, embed_model=None):
        # root for THIS workspace's companies — isolated per workspace
        self._root = storage_dir or config.STORAGE_DIR
        self._embed_model = embed_model
        self.storage_dir = os.path.join(self._root, "enterprise")
        os.makedirs(self.storage_dir, exist_ok=True)
        self.registry_path = os.path.join(self.storage_dir, "companies.pkl")
        self.companies: dict[str, CompanyMemory] = {}
        self._load_registry()

    def _new_company(self, company_id: str, company_name: str) -> CompanyMemory:
        return CompanyMemory(company_id, company_name,
                             root_storage_dir=self._root,
                             embed_model=self._embed_model)

    def _load_registry(self):
        if os.path.exists(self.registry_path):
            with open(self.registry_path, "rb") as f:
                registry = pickle.load(f)
            for company_id, company_name in registry.items():
                self.companies[company_id] = self._new_company(company_id, company_name)
            print(f"EnterpriseHub loaded: {len(self.companies)} companies")
        else:
            print("EnterpriseHub initialized fresh.")

    def _save_registry(self):
        registry = {cid: cm.company_name for cid, cm in self.companies.items()}
        with open(self.registry_path, "wb") as f:
            pickle.dump(registry, f)

    def add_company(self, company_id: str, company_name: str) -> CompanyMemory:
        if company_id in self.companies:
            print(f"Company already exists: {company_name}")
            return self.companies[company_id]
        cm = self._new_company(company_id, company_name)
        self.companies[company_id] = cm
        self._save_registry()
        print(f"Company added: {company_name} ({company_id})")
        return cm

    def get_company(self, company_id: str) -> Optional[CompanyMemory]:
        return self.companies.get(company_id)

    def list_companies(self) -> list:
        return [
            {
                "id": cid,
                "name": cm.company_name,
                "stats": cm.stats()
            }
            for cid, cm in self.companies.items()
        ]

    def query(self, company_id: str, question: str) -> str:
        cm = self.get_company(company_id)
        if not cm:
            return f"Company not found: {company_id}"
        return cm.query(question)

    # ────────────────────────────────────────────────────────────────────────
    # Task 5 — Cross-company patterns
    # ────────────────────────────────────────────────────────────────────────
    def get_anonymized_patterns(self) -> dict:
        """Aggregate decisions across all companies, grouped by community_used.
        Company names are stripped — only outcome patterns are exposed."""
        from collections import defaultdict
        bucket = defaultdict(lambda: {"total": 0, "success": 0, "failure": 0,
                                      "partial": 0, "unknown": 0,
                                      "question_samples": []})
        for cid, cm in self.companies.items():
            try:
                for nid, node in cm.company_dg.memory._all_nodes():
                    if not node.is_active: continue
                    for community in node.communities_used or []:
                        b = bucket[str(community)]
                        b["total"] += 1
                        outcome = node.outcome or "unknown"
                        b[outcome] = b.get(outcome, 0) + 1
                        if len(b["question_samples"]) < 5:
                            b["question_samples"].append(node.question[:120])
            except Exception as e:
                print(f"  Pattern collection skipped {cid}: {e}")
                continue
        out = {}
        for community_id, b in bucket.items():
            total = b["total"] or 1
            out[community_id] = {
                "total_decisions": b["total"],
                "success_rate":    round(b["success"] / total, 3),
                "failure_rate":    round(b["failure"] / total, 3),
                "partial_rate":    round(b["partial"] / total, 3),
                "unknown_rate":    round(b["unknown"] / total, 3),
                "question_samples": b["question_samples"],
            }
        return out

    def get_wisdom(self, question: str, company_id: str = None,
                    embed_model=None, top_k: int = 3) -> str:
        """Find patterns from OTHER companies relevant to `question`.
        Uses semantic similarity over their decision questions.
        Returns a human-readable summary string, or "" if nothing useful.
        """
        # gather decisions from all companies except `company_id`
        from collections import defaultdict
        peers = []
        for cid, cm in self.companies.items():
            if cid == company_id: continue
            try:
                for nid, node in cm.company_dg.memory._all_nodes():
                    if not node.is_active: continue
                    peers.append(node)
            except Exception:
                continue
        if not peers:
            return ""

        # if we have an embed model, semantically rank — otherwise fall back to
        # aggregate patterns by community
        if embed_model is not None:
            try:
                import numpy as np
                q_emb = embed_model.encode([question])[0]
                texts = [p.question for p in peers]
                embs = embed_model.encode(texts)
                norms = np.linalg.norm(embs, axis=1)
                q_norm = np.linalg.norm(q_emb) or 1.0
                sims = np.dot(embs, q_emb) / (norms * q_norm + 1e-9)
                ranked = sorted(zip(sims, peers), key=lambda r: r[0], reverse=True)[:top_k]
                lines = []
                for sim, p in ranked:
                    if sim < 0.35: continue
                    marker = {"success": "✓", "failure": "✗", "partial": "~"}.get(p.outcome, "·")
                    lines.append(f"  {marker} Similar Q (sim={sim:.2f}, outcome={p.outcome}): "
                                 f"\"{p.question[:120]}\" → {p.answer[:160]}")
                if not lines: return ""
                return "Patterns from peer companies (anonymised):\n" + "\n".join(lines)
            except Exception as e:
                print(f"  Wisdom semantic ranking failed, falling back: {e}")

        # Fallback — aggregate by communities
        patterns = self.get_anonymized_patterns()
        relevant = sorted(patterns.items(),
                          key=lambda kv: kv[1]["total_decisions"], reverse=True)[:top_k]
        if not relevant: return ""
        lines = [f"Aggregate patterns across peer companies:"]
        for cid, p in relevant:
            lines.append(f"  · Community {cid}: {p['total_decisions']} decisions, "
                         f"success={p['success_rate']:.0%}, failure={p['failure_rate']:.0%}")
        return "\n".join(lines)
