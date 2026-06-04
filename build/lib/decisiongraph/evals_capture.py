"""gbrain #9 — BrainBench-style eval capture + replay.

Every real query is appended (lightly redacted) to a per-workspace JSONL.
`replay()` re-runs the captured questions through ONLY the retrieval layer
(no LLM, no cost) and reports retrieval health — a regression signal for
"did a graph change quietly break recall?".
"""
from __future__ import annotations
import os, json, time, re
from datetime import datetime

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_NUM   = re.compile(r"\b\d[\d,]{3,}\b")          # long numbers (ids, $ figures)

def _redact(s: str) -> str:
    s = _EMAIL.sub("<email>", s or "")
    s = _NUM.sub("<num>", s)
    return s[:500]

def _path(storage_dir: str) -> str:
    return os.path.join(storage_dir, "query_evals.jsonl")

def capture(storage_dir: str, *, question: str, mode: str,
            intent: str = None, communities: int = 0, latency_ms: int = 0) -> None:
    try:
        os.makedirs(storage_dir, exist_ok=True)
        rec = {
            "ts": datetime.now().isoformat(),
            "q": _redact(question),
            "mode": mode, "intent": intent,
            "communities": communities, "latency_ms": latency_ms,
        }
        with open(_path(storage_dir), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass   # eval capture must never break a real query

def load(storage_dir: str, limit: int = 200) -> list:
    p = _path(storage_dir)
    if not os.path.exists(p):
        return []
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except Exception:
        pass
    return out[-limit:]

def replay(dg, storage_dir: str, sample: int = 30) -> dict:
    """Re-run captured questions through beam retrieval ONLY (no LLM).
    Reports what fraction still retrieve ≥1 community = retrieval health."""
    recs = load(storage_dir)
    if not recs:
        return {"captured": 0, "replayed": 0, "retrieval_rate": None,
                "note": "no captured queries yet"}
    if dg.G is None or not dg.summaries:
        return {"captured": len(recs), "replayed": 0, "retrieval_rate": None,
                "note": "no knowledge graph to replay against"}
    from .query import beam_query
    qs = [r["q"] for r in recs if r.get("q")][-sample:]
    hit = 0; total = 0; t0 = time.time()
    for q in qs:
        total += 1
        try:
            _, matched = beam_query(q, dg.G, dg.summaries,
                                    dg.community_ids, dg.community_embeddings,
                                    dg.embed_model)
            if matched:
                hit += 1
        except Exception:
            pass
    return {
        "captured": len(recs),
        "replayed": total,
        "retrieval_rate": round(hit / total, 3) if total else None,
        "elapsed_s": round(time.time() - t0, 2),
    }
