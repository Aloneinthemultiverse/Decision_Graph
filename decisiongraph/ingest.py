import re
import json
import pickle
import os
import time
import pdfplumber
import networkx as nx
from . import config

def read_pdf(path: str) -> str:
    text = ""
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
            if i % 20 == 0:
                print(f"  Read {i+1} pages...")
    print(f"  Total chars: {len(text)}")
    return text


def chunk_text(text: str, chunk_size: int = None, overlap: int = None):
    chunk_size = chunk_size or config.CHUNK_SIZE
    overlap = overlap or config.CHUNK_OVERLAP
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
    print(f"  Total chunks: {len(chunks)}")
    return chunks


def clean_chunk(text: str) -> str:
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def extract_triples_safe(text: str, client) -> list:
    text = clean_chunk(text)
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=config.LLM_MODEL,
                max_tokens=5000,
                messages=[{"role": "user", "content": f"""Extract (subject, relationship, object) triples from this text.
STRICT RULES:
- subject and object must be SHORT concept names, max 3 words
- No articles (a, the, that)
- Use clean nouns only
- Merge similar concepts
- relationship must be a short verb phrase, max 3 words
Return ONLY a valid JSON array with keys: subject, relationship, object.
No markdown, no explanation, just pure JSON array.
Text: {text}"""}]
            )
            raw = next(b.text for b in response.content if b.type == "text")
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if not match:
                time.sleep(1)
                continue
            raw = match.group(0)
            raw = re.sub(r',\s*]', ']', raw)
            raw = re.sub(r',\s*}', '}', raw)
            matches = re.findall(r'\{[^{}]+\}', raw)
            triples = []
            for m in matches:
                try:
                    obj = json.loads(m)
                    if all(k in obj for k in ["subject", "relationship", "object"]):
                        triples.append(obj)
                except:
                    continue
            if triples:
                return triples
        except Exception as e:
            print(f"  attempt {attempt+1} failed: {e}")
            time.sleep(1)
    return []


def extract_all_triples(chunks: list, checkpoint_path: str, client) -> list:
    if os.path.exists(checkpoint_path):
        with open(checkpoint_path, "rb") as f:
            state = pickle.load(f)
        all_triples = state["triples"]
        start_idx = state["chunk_idx"]
        print(f"  Resuming from chunk {start_idx}")
    else:
        all_triples = []
        start_idx = 0
        print("  Starting fresh")

    for i in range(start_idx, len(chunks)):
        triples = extract_triples_safe(chunks[i], client)
        all_triples.extend(triples)

        if i % 50 == 0:
            with open(checkpoint_path, "wb") as f:
                pickle.dump({"triples": all_triples, "chunk_idx": i}, f)
            print(f"  Chunk {i}/{len(chunks)} — {len(all_triples)} triples so far")

        time.sleep(0.5)

    with open(checkpoint_path, "wb") as f:
        pickle.dump({"triples": all_triples, "chunk_idx": len(chunks)}, f)
    print(f"  Done. Total triples: {len(all_triples)}")
    return all_triples


def build_graph(triples: list) -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    for t in triples:
        try:
            G.add_edge(t['subject'], t['object'], relation=t['relationship'])
        except:
            continue
    print(f"  Nodes: {G.number_of_nodes()}, Edges: {G.number_of_edges()}")
    return G


def merge_graphs(G_existing: nx.MultiDiGraph, G_new: nx.MultiDiGraph) -> nx.MultiDiGraph:
    G_merged = nx.MultiDiGraph()
    for u, v, data in G_existing.edges(data=True):
        G_merged.add_edge(u, v, relation=data['relation'])
    for u, v, data in G_new.edges(data=True):
        G_merged.add_edge(u, v, relation=data['relation'])
    print(f"  Merged — Nodes: {G_merged.number_of_nodes()}, Edges: {G_merged.number_of_edges()}")
    return G_merged
