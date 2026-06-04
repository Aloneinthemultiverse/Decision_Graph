import os
import anthropic
# Lazy import — sentence_transformers takes ~30s and pulls torch.
# Defer until something semantic is actually needed (query, blueprint).
# This drops MCP-server cold-boot from ~60s to <1s for the common case of
# code-graph-only tool use (find_callers, blast_radius, triage_pr, ...).
def _SentenceTransformer():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer

from . import config
from .ingest import read_pdf, chunk_text, extract_all_triples, build_graph, merge_graphs
from .graph import entity_resolution, detect_communities, summarize_communities, save_graph_state, load_graph_state
from .query import build_community_embeddings, beam_query
from .decisions import DecisionMemory
from .agent import react_agent
from .document_handlers import read_document


class DecisionGraph:
    def __init__(self, storage_dir: str = None, embed_model=None):
        # Per-instance storage dir → concurrency-safe isolation (no global mutation).
        self.storage_dir = storage_dir or config.STORAGE_DIR
        self.client = anthropic.Anthropic(
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_BASE_URL
        )
        # Embedding model is LAZY: loaded on first semantic access only.
        # `_embed_model_override` holds the injected instance (if any).
        # `embed_model` property below loads on demand and caches.
        self._embed_model_override = embed_model
        self._embed_model = None
        self.G = None
        self.communities = None
        self.summaries = None
        self.community_ids = None
        self.community_embeddings = None
        self.memory = DecisionMemory(storage_dir=self.storage_dir)

        # Decision memory loads INDEPENDENTLY of the knowledge graph — a
        # workspace can have decisions without ever ingesting documents
        # (storing a decision does not require a graph). Coupling these was a
        # latent bug: no graph file → graph-load throws → decisions silently
        # dropped on reload/eviction.
        try:
            self.memory.load()
        except Exception:
            pass

        # Knowledge-graph load is now LAZY. Cold-boot MCP server doesn't
        # touch embeddings → ~1s startup. _ensure_graph_loaded() fires on
        # first semantic access (query_knowledge, etc.)
        self._graph_loaded = False

    def _ensure_graph_loaded(self):
        """Lazy-load the semantic knowledge graph from disk. Idempotent."""
        if self._graph_loaded:
            return
        self._graph_loaded = True
        try:
            self.G, self.communities, self.summaries = load_graph_state(
                save_dir=self.storage_dir)
            self.community_ids, self.community_embeddings = (
                build_community_embeddings(self.summaries, self.embed_model))
            print(f"DecisionGraph loaded from {self.storage_dir}.")
        except Exception:
            print(f"DecisionGraph initialized fresh ({self.storage_dir}).")

    @property
    def embed_model(self):
        """Lazy-loaded embedding model. First access triggers SentenceTransformer
        load (~30s). Subsequent accesses are O(1)."""
        if self._embed_model is not None:
            return self._embed_model
        if self._embed_model_override is not None:
            self._embed_model = self._embed_model_override
            return self._embed_model
        ST = _SentenceTransformer()
        self._embed_model = ST(config.EMBED_MODEL)
        return self._embed_model

    def ingest(self, file_path: str):
        os.makedirs(self.storage_dir, exist_ok=True)
        file_name = os.path.splitext(os.path.basename(file_path))[0]
        checkpoint_path = os.path.join(self.storage_dir, f"checkpoint_{file_name}.pkl")

        print(f"\n{'='*50}")
        print(f"Ingesting: {file_path}")
        print(f"{'='*50}")

        print("\n[1/8] Reading document...")
        text = read_document(file_path)

        print("\n[2/8] Chunking...")
        chunks = chunk_text(text)

        print("\n[3/8] Extracting triples...")
        triples = extract_all_triples(chunks, checkpoint_path, self.client)

        print("\n[4/8] Building graph...")
        G_new = build_graph(triples)

        if self.G:
            print("\n[5/8] Merging with existing graph...")
            G_combined = merge_graphs(self.G, G_new)
        else:
            print("\n[5/8] No existing graph — using new graph...")
            G_combined = G_new

        print("\n[6/8] Entity resolution...")
        self.G, _ = entity_resolution(G_combined, self.embed_model)

        print("\n[7/8] Community detection...")
        self.communities = detect_communities(self.G)

        print("\n[8/8] Summarizing communities...")
        self.summaries = summarize_communities(self.communities, self.client)

        # build embeddings for beam search
        self.community_ids, self.community_embeddings = build_community_embeddings(
            self.summaries, self.embed_model
        )

        # save to THIS instance's dir
        save_graph_state(self.G, self.communities, self.summaries, save_dir=self.storage_dir)

        print(f"\n[OK] Ingestion complete.")
        print(f"   Nodes: {self.G.number_of_nodes()}")
        print(f"   Edges: {self.G.number_of_edges()}")
        print(f"   Communities: {len(self.communities)}")

    def query(self, question: str, mode: str = None) -> str:
        from . import config as cfg
        mode = mode or cfg.DEFAULT_QUERY_MODE

        # Lazy: load graph from disk on first query (no-op on subsequent calls)
        self._ensure_graph_loaded()
        if self.G is None:
            raise ValueError("No knowledge graph loaded. Call ingest() first.")

        if mode == cfg.QUERY_MODE_NORMAL:
            from .agent import normal_mode
            return normal_mode(
                question=question,
                client=self.client,
                memory=self.memory,
                embed_model=self.embed_model
            )

        elif mode == cfg.QUERY_MODE_SESSION:
            from .agent import session_mode
            return session_mode(
                question=question,
                client=self.client,
                G=self.G,
                community_summaries=self.summaries,
                community_ids=self.community_ids,
                community_embeddings=self.community_embeddings,
                embed_model=self.embed_model,
                memory=self.memory
            )

        else:  # deep mode
            return react_agent(
                question=question,
                client=self.client,
                G=self.G,
                community_summaries=self.summaries,
                community_ids=self.community_ids,
                community_embeddings=self.community_embeddings,
                embed_model=self.embed_model,
                memory=self.memory
            )

    def get_decisions(self) -> list:
        return self.memory.all_decisions()

    def stats(self) -> dict:
        return {
            "nodes": self.G.number_of_nodes() if self.G else 0,
            "edges": self.G.number_of_edges() if self.G else 0,
            "communities": len(self.communities) if self.communities else 0,
            "decisions": self.memory.count()
        }
