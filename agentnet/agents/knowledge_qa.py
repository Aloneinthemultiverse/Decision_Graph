"""Knowledge QA agent — sandboxed, network-less, deterministic retrieval.

SKILLS:
  * reads BOTH decisions (Q/A) and knowledge-graph content (triples, community
    summaries) from the scoped slice the host injected
  * given a task/question, scores every piece of evidence by token-overlap with
    the question (a tiny BM25-ish heuristic — pure stdlib, no LLM, no network)
  * returns: the top evidence (with kind + citation), a synthesized one-line
    answer assembled from the strongest hits, and counts per kind so the
    caller can see what was available

Contract: read /sandbox/input.json, write ONE JSON object to stdout.
"""
import json
import math
import re
from collections import Counter

STOP = set("the a an of and or to in on for with by is are was were be been "
           "being it its this that those these as at from into about over "
           "i you he she we they our your their what which who whose how "
           "why when where do does did will would can could should "
           "have has had not no yes if then else there here".split())


def tok(s: str):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in STOP and len(w) > 1]


def evidence_text(e: dict) -> str:
    k = e.get("kind")
    if k == "decision":
        return f"{e.get('question','')} {e.get('answer','')}"
    if k == "triple":
        return f"{e.get('subject','')} {e.get('relation','')} {e.get('object','')}"
    if k == "summary":
        return e.get("summary", "")
    return ""


def main():
    data = json.load(open("/sandbox/input.json"))
    task = data.get("task", "")
    scope = data.get("data", {}) or {}

    # flatten all evidence + remember which topic each came from
    all_ev = []
    by_kind = Counter()
    for topic, items in scope.items():
        for e in items or []:
            e = dict(e)
            e["_topic"] = topic
            all_ev.append(e)
            by_kind[e.get("kind", "?")] += 1

    q_tokens = tok(task)
    q_set = set(q_tokens)

    # idf-ish (tiny corpus, but it down-weights generic words across evidence)
    df = Counter()
    docs = []
    for e in all_ev:
        toks = tok(evidence_text(e))
        docs.append(toks)
        for t in set(toks):
            df[t] += 1
    N = max(1, len(docs))

    def score(toks):
        if not toks or not q_set:
            return 0.0
        tf = Counter(toks)
        s = 0.0
        for t in q_set:
            if t in tf:
                idf = math.log((N + 1) / (df[t] + 1)) + 1
                s += (1 + math.log(tf[t])) * idf
        # tiny boost for higher-confidence decisions
        return s

    scored = []
    for e, toks in zip(all_ev, docs):
        scored.append((score(toks), e))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [e for s, e in scored[:5] if s > 0]

    # craft a one-line answer from the strongest hit
    if top:
        best = top[0]
        if best.get("kind") == "decision":
            answer = best.get("answer") or best.get("question") or ""
            citation = f"decision (conf={best.get('confidence',0)})"
        elif best.get("kind") == "triple":
            answer = (f"{best.get('subject')} "
                      f"{best.get('relation')} {best.get('object')}").strip()
            citation = "knowledge graph triple"
        else:
            answer = (best.get("summary") or "")[:300]
            citation = f"community {best.get('community')}"
    else:
        answer = "No evidence in the approved scope matches the question."
        citation = "n/a"

    print(json.dumps({
        "task": task,
        "answer": answer,
        "citation": citation,
        "top_evidence": [
            {k: v for k, v in e.items() if k != "_topic"}
            | {"topic": e.get("_topic")}
            for e in top
        ],
        "scope_seen": {
            "total_evidence": len(all_ev),
            "by_kind": dict(by_kind),
            "topics": list(scope.keys()),
        },
        "summary": (f"Q: {task}\nA: {answer}\nSource: {citation} "
                    f"(scope had {len(all_ev)} pieces of evidence: "
                    f"{dict(by_kind)})"),
    }))


main()
