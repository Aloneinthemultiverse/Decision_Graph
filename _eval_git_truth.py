"""GIT-HISTORY GROUND TRUTH benchmark.

Idea: for each test commit C, look at the N follow-up commits that touched
ANY symbol defined in C's changed files. Those files are the *real* impact
set — the work humans actually did because of the change.

This removes the bias of grading tools against their own resolver output.
Both tools are now scored against an objective external reality.

For each test commit:
  1. Get files changed in C  (= the "edit")
  2. Walk forward N=50 commits
  3. For each follow-up commit F: if F touches a file that ALSO appears in
     the impact graph from C (same module / nearby file / same symbol name),
     count F's files as part of the actual impact set.

Simpler bootstrap: just take the UNION of files touched in any of the next
20 commits as a noisy proxy for "what eventually had to change because of C".
"""
from __future__ import annotations
import subprocess
from collections import Counter
from decisiongraph import code_graph as cg

REPOS = {
    "flask": {
        "path": r"C:\Users\SUJITN~1\AppData\Local\Temp\flask-bench",
        "db":   r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db",
        "commits": ["fbb6f0bc4c60a0bada0e03c3480d0ccf30a3c1df",
                    "a29f88ce6f2f9843bd6fcbbfce1390a2071965d6"],
    },
    "fastapi": {"path": r"C:\Users\SUJITN~1\AppData\Local\Temp\fastapi-bench",
                "db":   "storage/bench_graphs/fastapi.db",
                "commits": ["fa3588c38c7473aca7536b12d686102de4b0f407",
                            "0227991a01e61bf5cdd93cc00e9e243f52b47a4a"]},
    "httpx": {"path": r"C:\Users\SUJITN~1\AppData\Local\Temp\httpx-bench",
              "db":   "storage/bench_graphs/httpx.db",
              "commits": ["ae1b9f66238f75ced3ced5e4485408435de10768",
                          "b55d4635701d9dc22928ee647880c76b078ba3f2"]},
    "express": {"path": r"C:\Users\SUJITN~1\AppData\Local\Temp\express-bench",
                "db":   "storage/bench_graphs/express.db",
                "commits": ["925a1dff1e42f1b393c977b8b77757fcf633e09f",
                            "b4ab7d65d7724d9309b6faaaf82ad492da2a6d35"]},
    "gin": {"path": r"C:\Users\SUJITN~1\AppData\Local\Temp\gin-bench",
            "db":   "storage/bench_graphs/gin.db",
            "commits": ["052d1a79aafe3f04078a2716f8e77d4340308383",
                        "472d086af2acd924cb4b9d7be0525f7d790f69bc",
                        "5c00df8afadd06cc5be530dde00fe6d9fa4a2e4a"]},
    "code-review-graph":
        {"path": r"C:\Users\SUJITN~1\AppData\Local\Temp\code-review-graph-bench",
         "db":   "storage/bench_graphs/code-review-graph.db",
         "commits": ["528801f841e519567ef54d6e52e9b9831d162e1b",
                     "84bde35459c52e1e0c4b25c6c4799743021e0fc7"]},
}

def changed_in(repo, sha):
    r = subprocess.run(["git", "diff", "--name-only", f"{sha}~1", sha],
                       cwd=repo, capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]

def files_in_next_n_commits(repo_path, sha, n=20):
    """Files touched in the N commits AFTER `sha` on the same branch.
    Returns Counter of file_path → how often it was touched."""
    # list of follow-up commits — note --reverse + ancestry-path
    r = subprocess.run(
        ["git", "log", "--format=%H", f"{sha}..HEAD"],
        cwd=repo_path, capture_output=True, text=True)
    follow_shas = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    # follow_shas is newest-first by default; reverse to take the N closest-after
    follow_shas = list(reversed(follow_shas))[:n]
    counter: Counter[str] = Counter()
    for fs in follow_shas:
        r2 = subprocess.run(["git", "diff-tree", "--no-commit-id",
                             "--name-only", "-r", fs],
                            cwd=repo_path, capture_output=True, text=True)
        for line in r2.stdout.splitlines():
            line = line.strip()
            if line: counter[line] += 1
    return counter

def predicted(db, changed):
    p = set(changed)
    for f in changed:
        try:
            r = cg.blast_radius(db, f, max_depth=3, strict=True)
            p.update(r["affected_files"])
        except Exception: pass
    return p

def score(pred, actual):
    if not actual: return 0, 0.0, 0.0, 0.0
    tp = len(pred & actual)
    pr = tp / max(len(pred), 1)
    rc = tp / max(len(actual), 1)
    f1 = 2 * pr * rc / max(pr + rc, 0.001)
    return tp, pr, rc, f1

print(f"\n{'repo':<20} {'commit':<12} {'pred':>5} {'real':>5} {'TP':>3} {'P':>5} {'R':>5} {'F1':>5}")
print("-" * 70)
all_f1 = []
per_repo = {}
for repo, cfg in REPOS.items():
    f1s = []
    for sha in cfg["commits"]:
        ch = changed_in(cfg["path"], sha)
        if not ch: continue
        # ground truth = files touched in next 20 commits THAT WERE ALSO in the
        # neighbourhood of the changed files. To stay grounded, keep ALL files
        # touched within next 20 commits (noisy but objective).
        future_counter = files_in_next_n_commits(cfg["path"], sha, n=20)
        # filter: only files touched ≥1 times in the next 20 commits — this gives
        # us the union of "things people followed up on after this commit"
        actual = set(future_counter.keys()) | set(ch)
        pred = predicted(cfg["db"], ch)
        tp, p, r, f1 = score(pred, actual)
        f1s.append(f1); all_f1.append(f1)
        print(f"{repo:<20} {sha[:10]}   {len(pred):>5} {len(actual):>5} "
              f"{tp:>3} {p:>5.2f} {r:>5.2f} {f1:>5.2f}")
    per_repo[repo] = sum(f1s)/max(len(f1s),1)

print("\n" + "=" * 70)
print(f"\n{'repo':<20} {'git-truth F1':>14}")
print("-" * 40)
for r, f in per_repo.items():
    print(f"{r:<20} {f:>14.3f}")
print(f"\nOVERALL git-truth F1 = {sum(all_f1)/max(len(all_f1),1):.3f}")
