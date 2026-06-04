"""Mirror code-review-graph's impact_accuracy.py eval against OUR blast_radius.

Methodology (matches theirs):
  actual    = changed_files ∪ {files whose symbols call-into or import-from any
                                symbol defined in changed_files}  (1-hop reverse)
  predicted = changed_files ∪ {files in blast_radius(f) for each changed f}
  F1 = harmonic mean of P/R against actual.

Graph already built at storage/workspaces/p4jIEgrAJd33s3qH-DZbhQ/personal/code_graph.db
(flask @ a29f88ce — the pinned snapshot SHA from their flask.yaml).
"""
from __future__ import annotations
import subprocess, sqlite3
from pathlib import Path
from decisiongraph import code_graph as cg

DB   = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
REPO = Path(r"C:\tmp_dg")  # local flask checkout
FLASK = Path(r"C:\Users\SUJITN~1\AppData\Local\Temp\flask-bench")

TEST_COMMITS = [
    "fbb6f0bc4c60a0bada0e03c3480d0ccf30a3c1df",
    "a29f88ce6f2f9843bd6fcbbfce1390a2071965d6",
]

def get_changed_files(sha: str) -> list[str]:
    r = subprocess.run(["git", "diff", "--name-only", f"{sha}~1", sha],
                       cwd=str(FLASK), capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]

def actual_set(changed: list[str], conn) -> set[str]:
    """1-hop reverse: who calls into / imports from changed files."""
    actual = set(changed)
    # symbols defined in any changed file
    placeholders = ",".join("?" for _ in changed)
    sym_rows = conn.execute(
        f"SELECT id, name FROM symbols WHERE path IN ({placeholders})",
        changed).fetchall()
    sym_ids   = [r["id"]   for r in sym_rows]
    sym_names = [r["name"] for r in sym_rows]
    # callers
    if sym_ids:
        ph = ",".join("?" for _ in sym_ids)
        rows = conn.execute(
            f"SELECT DISTINCT s.path FROM calls c JOIN symbols s ON c.src_id=s.id "
            f"WHERE c.dst_id IN ({ph})", sym_ids).fetchall()
        actual.update(r["path"] for r in rows)
    if sym_names:
        ph = ",".join("?" for _ in sym_names)
        rows = conn.execute(
            f"SELECT DISTINCT s.path FROM calls c JOIN symbols s ON c.src_id=s.id "
            f"WHERE c.dst_name IN ({ph})", sym_names).fetchall()
        actual.update(r["path"] for r in rows)
    # importers
    for f in changed:
        stem = Path(f).stem
        rows = conn.execute(
            "SELECT DISTINCT src_path FROM imports WHERE dst_module LIKE ? OR dst_module LIKE ?",
            (f"%{stem}%", f"%{f}%")).fetchall()
        actual.update(r["src_path"] for r in rows)
    return actual

def predicted_set(changed: list[str]) -> set[str]:
    predicted = set(changed)
    for f in changed:
        r = cg.blast_radius(DB, f, max_depth=3)
        predicted.update(r["affected_files"])
    return predicted

def f1(predicted: set, actual: set):
    tp = len(predicted & actual)
    p  = tp / max(len(predicted), 1)
    r  = tp / max(len(actual), 1)
    f1 = 2*p*r / max(p+r, 0.001)
    return tp, p, r, f1

if __name__ == "__main__":
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    print(f"{'commit':<12} {'pred':>5} {'actual':>7} {'TP':>4} {'P':>6} {'R':>6} {'F1':>6}")
    f1s = []
    for sha in TEST_COMMITS:
        changed = get_changed_files(sha)
        actual    = actual_set(changed, conn)
        predicted = predicted_set(changed)
        tp, p, r, fs = f1(predicted, actual)
        f1s.append(fs)
        print(f"{sha[:10]}   {len(predicted):>5} {len(actual):>7} {tp:>4} "
              f"{p:>6.3f} {r:>6.3f} {fs:>6.3f}")
    print(f"\nAVG F1 = {sum(f1s)/len(f1s):.3f}   (theirs: 0.628)")
