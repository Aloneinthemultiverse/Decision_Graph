"""Run impact-accuracy F1 across all 5 repos × 13 commits.

Same methodology as code-review-graph's eval/benchmarks/impact_accuracy.py.
"""
from __future__ import annotations
import subprocess, sqlite3
from pathlib import Path
from decisiongraph import code_graph as cg

REPOS = {
    "fastapi":           {"db":"storage/bench_graphs/fastapi.db",
                          "path":r"C:\Users\SUJITN~1\AppData\Local\Temp\fastapi-bench",
                          "commits":["fa3588c38c7473aca7536b12d686102de4b0f407",
                                     "0227991a01e61bf5cdd93cc00e9e243f52b47a4a"]},
    "flask":             {"db":r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db",
                          "path":r"C:\Users\SUJITN~1\AppData\Local\Temp\flask-bench",
                          "commits":["fbb6f0bc4c60a0bada0e03c3480d0ccf30a3c1df",
                                     "a29f88ce6f2f9843bd6fcbbfce1390a2071965d6"]},
    "httpx":             {"db":"storage/bench_graphs/httpx.db",
                          "path":r"C:\Users\SUJITN~1\AppData\Local\Temp\httpx-bench",
                          "commits":["ae1b9f66238f75ced3ced5e4485408435de10768",
                                     "b55d4635701d9dc22928ee647880c76b078ba3f2"]},
    "express":           {"db":"storage/bench_graphs/express.db",
                          "path":r"C:\Users\SUJITN~1\AppData\Local\Temp\express-bench",
                          "commits":["925a1dff1e42f1b393c977b8b77757fcf633e09f",
                                     "b4ab7d65d7724d9309b6faaaf82ad492da2a6d35"]},
    "gin":               {"db":"storage/bench_graphs/gin.db",
                          "path":r"C:\Users\SUJITN~1\AppData\Local\Temp\gin-bench",
                          "commits":["052d1a79aafe3f04078a2716f8e77d4340308383",
                                     "472d086af2acd924cb4b9d7be0525f7d790f69bc",
                                     "5c00df8afadd06cc5be530dde00fe6d9fa4a2e4a"]},
    "code-review-graph": {"db":"storage/bench_graphs/code-review-graph.db",
                          "path":r"C:\Users\SUJITN~1\AppData\Local\Temp\code-review-graph-bench",
                          "commits":["528801f841e519567ef54d6e52e9b9831d162e1b",
                                     "84bde35459c52e1e0c4b25c6c4799743021e0fc7"]},
}

# Their published F1 averages from README:
THEIRS = {"fastapi":0.834, "flask":0.628, "httpx":0.864, "express":0.667,
          "gin":0.609, "code-review-graph":None}

def changed_files(repo_path, sha):
    r = subprocess.run(["git","diff","--name-only",f"{sha}~1",sha],
                       cwd=repo_path, capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]

def actual_set(changed, db):
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    actual = set(changed)
    if not changed: return actual
    ph = ",".join("?" for _ in changed)
    sym_rows = conn.execute(
        f"SELECT id,name FROM symbols WHERE path IN ({ph})", changed).fetchall()
    sym_ids   = [r["id"]   for r in sym_rows]
    sym_names = [r["name"] for r in sym_rows]
    if sym_ids:
        p = ",".join("?" for _ in sym_ids)
        for r in conn.execute(
            f"SELECT DISTINCT s.path FROM calls c JOIN symbols s ON c.src_id=s.id "
            f"WHERE c.dst_id IN ({p})", sym_ids).fetchall():
            actual.add(r["path"])
    if sym_names:
        p = ",".join("?" for _ in sym_names)
        for r in conn.execute(
            f"SELECT DISTINCT s.path FROM calls c JOIN symbols s ON c.src_id=s.id "
            f"WHERE c.dst_name IN ({p})", sym_names).fetchall():
            actual.add(r["path"])
    for f in changed:
        stem = Path(f).stem
        for r in conn.execute(
            "SELECT DISTINCT src_path FROM imports WHERE dst_module LIKE ? OR dst_module LIKE ?",
            (f"%{stem}%", f"%{f}%")).fetchall():
            actual.add(r["src_path"])
    conn.close()
    return actual

def predicted_set(changed, db):
    p = set(changed)
    for f in changed:
        try:
            r = cg.blast_radius(db, f, max_depth=3)
            p.update(r["affected_files"])
        except Exception: pass
    return p

def score(predicted, actual):
    tp = len(predicted & actual)
    pr = tp / max(len(predicted), 1)
    rc = tp / max(len(actual), 1)
    f1 = 2*pr*rc / max(pr+rc, 0.001)
    return tp, pr, rc, f1

print(f"\n{'repo':<20} {'commit':<12} {'pred':>5} {'actual':>7} {'TP':>4} {'P':>6} {'R':>6} {'F1':>6}")
print("-"*78)
all_f1 = []; per_repo = {}
for repo, cfg in REPOS.items():
    repo_f1 = []
    for sha in cfg["commits"]:
        ch = changed_files(cfg["path"], sha)
        if not ch: continue
        a = actual_set(ch, cfg["db"])
        p = predicted_set(ch, cfg["db"])
        tp, pr, rc, f1 = score(p, a)
        repo_f1.append(f1); all_f1.append(f1)
        print(f"{repo:<20} {sha[:10]}   {len(p):>5} {len(a):>7} {tp:>4} {pr:>6.3f} {rc:>6.3f} {f1:>6.3f}")
    per_repo[repo] = sum(repo_f1)/max(len(repo_f1),1)

print("\n" + "="*78)
print(f"\n{'repo':<20} {'ours_F1':>10} {'theirs_F1':>10}  delta")
print("-"*78)
for r, ours in per_repo.items():
    theirs = THEIRS.get(r)
    d = f"{ours-theirs:+.3f}" if theirs else "—"
    t = f"{theirs:.3f}" if theirs else "—"
    print(f"{r:<20} {ours:>10.3f} {t:>10}  {d}")
print(f"\nOVERALL  ours={sum(all_f1)/len(all_f1):.3f}   theirs=0.714 (13-commit avg)")
