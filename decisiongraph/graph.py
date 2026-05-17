import pickle
import os
import time
import numpy as np
import networkx as nx
import community as community_louvain
from sentence_transformers import SentenceTransformer
from . import config


def entity_resolution(G: nx.MultiDiGraph, embed_model, threshold: float = None):
    threshold = threshold or config.ENTITY_RESOLUTION_THRESHOLD
    nodes = list(G.nodes())
    print(f"  Embedding {len(nodes)} nodes...")
    embeddings = embed_model.encode(nodes, show_progress_bar=False)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    normalized = embeddings / norms
    merged = {}
    processed = set()

    for i in range(len(nodes)):
        if nodes[i] in processed:
            continue
        group = [nodes[i]]
        processed.add(nodes[i])
        sims = np.dot(normalized[i], normalized[i+1:].T)
        for j, sim in enumerate(sims):
            actual_j = i + 1 + j
            if sim >= threshold and nodes[actual_j] not in processed:
                group.append(nodes[actual_j])
                processed.add(nodes[actual_j])
        canonical = min(group, key=lambda x: len(x))
        for node in group:
            merged[node] = canonical

    new_G = nx.MultiDiGraph()
    for u, v, data in G.edges(data=True):
        clean_u = merged.get(u, u)
        clean_v = merged.get(v, v)
        if clean_u != clean_v:
            new_G.add_edge(clean_u, clean_v, relation=data['relation'])

    print(f"  After resolution — Nodes: {new_G.number_of_nodes()}, Edges: {new_G.number_of_edges()}")
    return new_G, merged


def detect_communities(G: nx.MultiDiGraph, min_size: int = None):
    min_size = min_size or config.COMMUNITY_MIN_SIZE
    G_undirected = G.to_undirected()
    partition = community_louvain.best_partition(G_undirected)
    communities = {}
    for node, cid in partition.items():
        if cid not in communities:
            communities[cid] = []
        communities[cid].append(node)

    large = {cid: nodes for cid, nodes in communities.items() if len(nodes) >= min_size}
    small = {cid: nodes for cid, nodes in communities.items() if len(nodes) < min_size}

    for cid, nodes in small.items():
        best = None
        best_count = 0
        G_und = G.to_undirected()
        for node in nodes:
            for neighbor in G_und.neighbors(node):
                for large_cid, large_nodes in large.items():
                    if neighbor in large_nodes:
                        best_count += 1
                        best = large_cid
        if best:
            large[best].extend(nodes)
        else:
            largest = max(large, key=lambda x: len(large[x]))
            large[largest].extend(nodes)

    print(f"  Communities: {len(large)}")
    return large


def summarize_communities(communities: dict, client) -> dict:
    summaries = {}
    total = len(communities)
    for i, (cid, nodes) in enumerate(communities.items()):
        node_list = ", ".join(nodes[:10])
        response = client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": f"""These concepts form a related cluster:
{node_list}
Write ONE short sentence max 15 words summarizing this cluster."""}]
        )
        summary = next(b.text for b in response.content if b.type == "text")
        summaries[cid] = {"nodes": nodes, "summary": summary}
        print(f"  [{i+1}/{total}] Community {cid}: {summary[:60]}...")
        time.sleep(0.3)
    return summaries


def save_graph_state(G, communities, summaries, save_dir: str = None):
    save_dir = save_dir or config.STORAGE_DIR
    os.makedirs(save_dir, exist_ok=True)
    with open(f"{save_dir}/graph_clean.pkl", "wb") as f:
        pickle.dump(G, f)
    with open(f"{save_dir}/communities_clean.pkl", "wb") as f:
        pickle.dump(communities, f)
    with open(f"{save_dir}/summaries_clean.pkl", "wb") as f:
        pickle.dump(summaries, f)
    print(f"  Graph state saved to {save_dir}")


def load_graph_state(save_dir: str = None):
    save_dir = save_dir or config.STORAGE_DIR
    with open(f"{save_dir}/graph_clean.pkl", "rb") as f:
        G = pickle.load(f)
    with open(f"{save_dir}/communities_clean.pkl", "rb") as f:
        communities = pickle.load(f)
    with open(f"{save_dir}/summaries_clean.pkl", "rb") as f:
        summaries = pickle.load(f)
    print(f"  Graph loaded — Nodes: {G.number_of_nodes()}, Communities: {len(communities)}")
    return G, communities, summaries
