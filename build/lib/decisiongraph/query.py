import re
import numpy as np
from . import config

_STOP = set("the a an of to in on for and or is are was were be been being with "
            "as at by from this that these those it its we our you your they their "
            "what which who how why when where do does did will would should can "
            "could about into over under than then them he she his her".split())

def _tokens(s: str) -> list:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in _STOP and len(w) > 2]


# ── gbrain #2: keyword search + RRF fusion ────────────────────────────────────
def keyword_search(question: str, G, limit: int = 60) -> list:
    """Cheap BM25-ish keyword pass over graph edges (no deps, no LLM).
    Returns ranked triple strings."""
    if G is None or G.number_of_edges() == 0:
        return []
    qtok = set(_tokens(question))
    if not qtok:
        return []
    scored = []
    for u, v, data in G.edges(data=True):
        rel = data.get("relation", "")
        triple = f"{u} --[{rel}]--> {v}"
        ttok = set(_tokens(f"{u} {rel} {v}"))
        if not ttok:
            continue
        overlap = len(qtok & ttok)
        if overlap:
            # length-normalised overlap (rewards specific matches)
            scored.append((overlap / (1 + np.log1p(len(ttok))), triple))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored[:limit]]


def rrf_fuse(*rankings, k: int = 60, limit: int = 60) -> list:
    """Reciprocal Rank Fusion: score = Σ 1/(k+rank). Merges any number of
    ranked lists of strings into one consensus ranking."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return [it for it, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]]


# ── gbrain #8: intent classifier (heuristic, zero-LLM) ────────────────────────
def classify_intent(question: str) -> str:
    """Route a question to a retrieval strategy: temporal | entity | event | general."""
    q = (question or "").lower()
    if re.search(r"\b(when|timeline|history|over time|before|after|recent|latest|"
                 r"last (week|month|year|quarter)|trend|evolv)", q):
        return "temporal"
    if re.search(r"\b(who|which (company|team|person|vendor)|owns|reports to|"
                 r"works (at|for)|responsible|stakeholder)", q):
        return "entity"
    if re.search(r"\b(what happened|incident|outcome|result of|decision (to|on|about)|"
                 r"why did|postmortem)", q):
        return "event"
    return "general"


def intent_to_mode(intent: str) -> str:
    """Map detected intent to one of DecisionGraph's query modes."""
    return {
        "temporal": "deep",     # needs multi-step traversal across time
        "entity":   "session",  # graph-centric single pass
        "event":    "deep",     # causal/decision chains
        "general":  "session",
    }.get(intent, "session")


def build_community_embeddings(community_summaries: dict, embed_model):
    community_ids = list(community_summaries.keys())
    community_texts = [community_summaries[cid]["summary"] for cid in community_ids]
    community_embeddings = embed_model.encode(community_texts)
    return community_ids, community_embeddings


def beam_query(question: str, G, community_summaries: dict,
               community_ids: list, community_embeddings,
               embed_model, k: int = None):
    k = k or config.BEAM_K
    q_embedding = embed_model.encode([question])[0]
    norms = np.linalg.norm(community_embeddings, axis=1)
    q_norm = np.linalg.norm(q_embedding)
    sims = np.dot(community_embeddings, q_embedding) / (norms * q_norm)
    top_k_idx = np.argsort(sims)[::-1][:k]

    all_context = []
    matched = []

    for idx in top_k_idx:
        cid = community_ids[idx]
        nodes = community_summaries[cid]["nodes"]
        summary = community_summaries[cid]["summary"]
        print(f"  Beam — Community {cid} (sim={sims[idx]:.3f}): {summary[:60]}...")
        matched.append(cid)

        for node in nodes[:15]:
            for u, v, data in G.edges(node, data=True):
                all_context.append(f"{u} --[{data['relation']}]--> {v}")
            for u, v, data in G.in_edges(node, data=True):
                all_context.append(f"{u} --[{data['relation']}]--> {v}")

    return list(set(all_context))[:60], matched


def check_uncertainty(
    question: str,
    community_embeddings,
    community_ids: list,
    community_summaries: dict,
    embed_model,
    threshold: float = 0.3,
) -> dict:
    """Compare the question to all community summaries. If the max cosine
    similarity is below `threshold`, the graph has no obviously-relevant
    content — the caller should refuse to answer instead of hallucinating.

    Returns:
        {"uncertain": bool, "confidence": float, "message": str?, "suggestion": str?}
    """
    # Empty / unready graph → uncertain, but with explanatory message
    if (community_embeddings is None or community_ids is None
            or len(community_ids) == 0 or not community_summaries):
        return {
            "uncertain": True,
            "confidence": 0.0,
            "message": "I don't have any knowledge graph loaded yet to answer this confidently.",
            "suggestion": "Ingest documents related to the topic before querying.",
        }

    q_emb = embed_model.encode([question])[0]
    norms = np.linalg.norm(community_embeddings, axis=1)
    q_norm = np.linalg.norm(q_emb) or 1.0
    sims = np.dot(community_embeddings, q_emb) / (norms * q_norm + 1e-9)
    max_idx = int(np.argmax(sims))
    max_sim = float(sims[max_idx])

    if max_sim < threshold:
        # extract the closest topic so the suggestion is useful
        closest = community_summaries.get(community_ids[max_idx], {}).get("summary", "")
        closest_short = " ".join(closest.split()[:12])
        return {
            "uncertain": True,
            "confidence": round(max_sim, 3),
            "message": (
                f"I don't have enough context to answer confidently. "
                f"The closest topic in the graph (similarity={max_sim:.2f}) is far from your question."
            ),
            "suggestion": (
                f"Consider ingesting documents about: '{question[:80]}'. "
                f"Closest existing topic was: \"{closest_short}…\"" if closest_short else
                f"Consider ingesting documents about: '{question[:80]}'."
            ),
        }
    return {"uncertain": False, "confidence": round(max_sim, 3)}


def multi_graph_beam_query(
    question: str,
    graphs: list,        # [{"name": str, "G": graph, "community_summaries": dict, "community_ids": list, "community_embeddings": array}]
    embed_model,
    k: int = None,
) -> tuple:
    """Run beam search across multiple graphs simultaneously, merge results.

    Each result triple is tagged with its source graph: "[Graph Name] {triple}".
    Deduplicates by raw triple content — if the same fact appears in two graphs,
    the first occurrence's source tag wins.

    Returns:
        (tagged_context_triples, hit_report)
        hit_report = {
            "<graph_name>": {
                "communities": [cid, ...],
                "triples": int,        # how many unique triples this graph contributed
                "skipped": str | None  # reason if the graph was skipped (empty, etc.)
            }, ...
        }
    """
    k = k or config.BEAM_K
    all_tagged: list[str] = []
    seen_raw: set[str] = set()
    hit_report: dict[str, dict] = {}

    for g in graphs or []:
        name = g.get("name") or "graph"
        G = g.get("G")
        comms = g.get("community_summaries")
        cids  = g.get("community_ids")
        cembs = g.get("community_embeddings")

        # skip empty / unready graphs cleanly
        if G is None or G.number_of_nodes() == 0:
            hit_report[name] = {"communities": [], "triples": 0, "skipped": "graph is empty"}
            continue
        if not comms or cids is None or cembs is None or len(cids) == 0:
            hit_report[name] = {"communities": [], "triples": 0, "skipped": "no communities"}
            continue

        try:
            triples, matched = beam_query(question, G, comms, cids, cembs, embed_model, k)
        except Exception as e:
            hit_report[name] = {"communities": [], "triples": 0, "skipped": f"error: {e}"}
            continue

        added = 0
        for t in triples:
            if t in seen_raw:
                continue
            seen_raw.add(t)
            all_tagged.append(f"[{name}] {t}")
            added += 1

        hit_report[name] = {"communities": matched, "triples": added, "skipped": None}

    return all_tagged, hit_report
