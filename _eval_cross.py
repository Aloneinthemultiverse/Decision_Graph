"""CROSS-TOOL F1: score OUR predicted_set against THEIR actual_set.

Same `actual` ground-truth set built from THEIR graph (so both tools score
against an identical reference). `predicted` comes from OUR blast_radius.
This is the honest head-to-head: removes the self-consistency caveat.
"""
from __future__ import annotations
import subprocess, sqlite3, os
from pathlib import Path
from code_review_graph.graph import GraphStore
from decisiongraph import code_graph as cg

REPOS = {
    "flask":             {"path": r"C:\Users\SUJITN~1\AppData\Local\Temp\flask-bench",
                          "our_db": r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db",
                          "commits": ["fbb6f0bc4c60a0bada0e03c3480d0ccf30a3c1df",
                                      "a29f88ce6f2f9843bd6fcbbfce1390a2071965d6"]},
    # add more after we prove flask works
}

def changed_files(repo_path, sha):
    r = subprocess.run(["git","diff","--name-only",f"{sha}~1",sha],
                       cwd=repo_path, capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]

def their_actual_set(store, changed):
    """Mirror their impact_accuracy.py actual_set computation."""
    actual = set(changed)
    for f in changed:
        nodes = store.get_nodes_by_file(f)
        for node in nodes:
            for edge in store.get_edges_by_target(node.qualified_name):
                if edge.kind in ("CALLS", "IMPORTS_FROM"):
                    src_qual = edge.source_qualified
                    src_file = src_qual.split("::")[0] if "::" in src_qual else ""
                    if src_file: actual.add(src_file)
    return actual

def our_predicted_set(db, changed):
    # 1-hop to match their `actual_set` methodology (1-hop reverse edges).
    p = set(changed)
    for f in changed:
        try:
            r = cg.blast_radius(db, f, max_depth=1, strict=True)
            p.update(r["affected_files"])
        except Exception: pass
    return p

def score(pred, actual):
    tp = len(pred & actual)
    pr = tp / max(len(pred),1)
    rc = tp / max(len(actual),1)
    f1 = 2*pr*rc / max(pr+rc, 0.001)
    return tp, pr, rc, f1

print(f"\n{'repo':<10} {'commit':<12} {'pred(us)':>9} {'actual(them)':>13} {'TP':>4} {'P':>6} {'R':>6} {'F1':>6}")
print("-"*72)
all_f1 = []
for repo, cfg in REPOS.items():
    their_db = os.path.join(cfg["path"], ".code-review-graph", "graph.db")
    store = GraphStore(their_db)
    for sha in cfg["commits"]:
        ch = changed_files(cfg["path"], sha)
        if not ch: continue
        actual = their_actual_set(store, ch)
        pred   = our_predicted_set(cfg["our_db"], ch)
        tp, pr, rc, f1 = score(pred, actual)
        all_f1.append(f1)
        print(f"{repo:<10} {sha[:10]}   {len(pred):>9} {len(actual):>13} {tp:>4} {pr:>6.3f} {rc:>6.3f} {f1:>6.3f}")

print(f"\nCROSS-TOOL AVG F1 = {sum(all_f1)/max(len(all_f1),1):.3f}")
