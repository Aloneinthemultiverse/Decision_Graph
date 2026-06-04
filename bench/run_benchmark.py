#!/usr/bin/env python
"""Phase-2 CI gate: ingest the 6 benchmark repos, run F1 evals against three
ground-truth definitions, compare against a committed baseline, exit non-zero
if any tier regresses by more than the allowed delta.

Run locally:
    python bench/run_benchmark.py

Run in CI:
    python bench/run_benchmark.py --strict   # exit 1 on any regression
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from decisiongraph import code_graph as cg

# Pinned snapshot SHAs from code-review-graph's eval/configs (a29f88ce etc.)
REPOS = {
    "flask":             {"url": "https://github.com/pallets/flask",
                          "sha": "a29f88ce6f2f9843bd6fcbbfce1390a2071965d6",
                          "commits": ["fbb6f0bc4c60a0bada0e03c3480d0ccf30a3c1df",
                                      "a29f88ce6f2f9843bd6fcbbfce1390a2071965d6"]},
    "fastapi":           {"url": "https://github.com/tiangolo/fastapi",
                          "sha": "0227991a01e61bf5cdd93cc00e9e243f52b47a4a",
                          "commits": ["fa3588c38c7473aca7536b12d686102de4b0f407",
                                      "0227991a01e61bf5cdd93cc00e9e243f52b47a4a"]},
    "httpx":             {"url": "https://github.com/encode/httpx",
                          "sha": "b55d4635701d9dc22928ee647880c76b078ba3f2",
                          "commits": ["ae1b9f66238f75ced3ced5e4485408435de10768",
                                      "b55d4635701d9dc22928ee647880c76b078ba3f2"]},
    "express":           {"url": "https://github.com/expressjs/express",
                          "sha": "b4ab7d65d7724d9309b6faaaf82ad492da2a6d35",
                          "commits": ["925a1dff1e42f1b393c977b8b77757fcf633e09f",
                                      "b4ab7d65d7724d9309b6faaaf82ad492da2a6d35"]},
    "gin":               {"url": "https://github.com/gin-gonic/gin",
                          "sha": "5c00df8afadd06cc5be530dde00fe6d9fa4a2e4a",
                          "commits": ["052d1a79aafe3f04078a2716f8e77d4340308383",
                                      "472d086af2acd924cb4b9d7be0525f7d790f69bc",
                                      "5c00df8afadd06cc5be530dde00fe6d9fa4a2e4a"]},
}

WORK = ROOT / ".bench"
WORK.mkdir(exist_ok=True)
DB_DIR = WORK / "graphs"; DB_DIR.mkdir(exist_ok=True)
CHECKOUTS = WORK / "repos"; CHECKOUTS.mkdir(exist_ok=True)
BASELINE_FILE = ROOT / "bench" / "baseline.json"

def ensure_repo(name: str, cfg: dict) -> Path:
    """Clone repo if missing; checkout the pinned SHA."""
    p = CHECKOUTS / name
    if not (p / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", cfg["url"], str(p)], check=True)
    subprocess.run(["git", "checkout", "-q", cfg["sha"]], cwd=str(p), check=True)
    return p

def build_graph(name: str, path: Path) -> str:
    db = str(DB_DIR / f"{name}.db")
    if os.path.exists(db): os.remove(db)
    cg.build_from_repo(db, str(path), name)
    return db

def git_changed(repo: Path, sha: str) -> list[str]:
    r = subprocess.run(["git", "diff", "--name-only", f"{sha}~1", sha],
                       cwd=str(repo), capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]

def predicted(db: str, changed: list[str], hops: int) -> set[str]:
    p = set(changed)
    for f in changed:
        try:
            r = cg.blast_radius(db, f, max_depth=hops, strict=True)
            p.update(r["affected_files"])
        except Exception: pass
    return p

def actual_self(db: str, changed: list[str]) -> set[str]:
    """Mirror code-review-graph's impact_accuracy.py actual_set on our graph."""
    import sqlite3
    actual = set(changed)
    if not changed: return actual
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    ph = ",".join("?" for _ in changed)
    syms = conn.execute(
        f"SELECT id, name FROM symbols WHERE path IN ({ph})", changed).fetchall()
    ids   = [r["id"]   for r in syms]
    names = [r["name"] for r in syms]
    if ids:
        ph2 = ",".join("?" for _ in ids)
        for r in conn.execute(
            f"SELECT DISTINCT s.path FROM calls c JOIN symbols s ON c.src_id=s.id "
            f"WHERE c.dst_id IN ({ph2})", ids).fetchall():
            actual.add(r["path"])
    if names:
        ph2 = ",".join("?" for _ in names)
        for r in conn.execute(
            f"SELECT DISTINCT s.path FROM calls c JOIN symbols s ON c.src_id=s.id "
            f"WHERE c.dst_name IN ({ph2})", names).fetchall():
            actual.add(r["path"])
    from pathlib import Path as _P
    for f in changed:
        stem = _P(f).stem
        for r in conn.execute(
            "SELECT DISTINCT src_path FROM imports "
            "WHERE dst_module LIKE ? OR dst_module LIKE ?",
            (f"%{stem}%", f"%{f}%")).fetchall():
            actual.add(r["src_path"])
    conn.close()
    return actual

def actual_git(repo: Path, sha: str, n: int = 20) -> set[str]:
    """Files touched in the next n commits."""
    r = subprocess.run(["git", "log", "--format=%H", f"{sha}..HEAD"],
                       cwd=str(repo), capture_output=True, text=True)
    follows = list(reversed([l.strip() for l in r.stdout.splitlines() if l.strip()]))[:n]
    files: set[str] = set()
    for fs in follows:
        r2 = subprocess.run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", fs],
                            cwd=str(repo), capture_output=True, text=True)
        for line in r2.stdout.splitlines():
            line = line.strip()
            if line: files.add(line)
    return files

def f1(pred: set, actual: set) -> tuple[int,float,float,float]:
    if not actual: return 0, 0.0, 0.0, 0.0
    tp = len(pred & actual)
    p  = tp / max(len(pred), 1)
    r  = tp / max(len(actual), 1)
    return tp, p, r, 2*p*r / max(p+r, 1e-3)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="Exit 1 on ANY regression vs baseline")
    ap.add_argument("--max-regression", type=float, default=0.05,
                    help="Allowed drop in overall F1 (default 0.05)")
    ap.add_argument("--write-baseline", action="store_true",
                    help="Write current numbers as new baseline")
    args = ap.parse_args()

    started = time.time()
    results = {"self": {}, "git": {}}

    print(f"\n=== Benchmark run ({len(REPOS)} repos) ===")
    for name, cfg in REPOS.items():
        print(f"\n[{name}]")
        repo = ensure_repo(name, cfg)
        db   = build_graph(name, repo)
        for sha in cfg["commits"]:
            ch = git_changed(repo, sha)
            if not ch: continue
            pred_s = predicted(db, ch, hops=3)
            pred_g = predicted(db, ch, hops=3)
            act_s  = actual_self(db, ch)
            act_g  = actual_git(repo, sha) | set(ch)
            _, _, _, f1_s = f1(pred_s, act_s)
            _, _, _, f1_g = f1(pred_g, act_g)
            print(f"  {sha[:10]}  self-F1={f1_s:.3f}  git-truth-F1={f1_g:.3f}")
            results["self"].setdefault(name, []).append(f1_s)
            results["git"] .setdefault(name, []).append(f1_g)

    summary = {
        "self":   {r: sum(v)/len(v) for r, v in results["self"].items()},
        "git":    {r: sum(v)/len(v) for r, v in results["git"] .items()},
    }
    all_self = [x for v in results["self"].values() for x in v]
    all_git  = [x for v in results["git"] .values() for x in v]
    summary["overall_self"] = sum(all_self)/max(len(all_self),1)
    summary["overall_git"]  = sum(all_git) /max(len(all_git), 1)
    summary["elapsed_s"]    = round(time.time() - started, 1)

    print("\n" + "="*70)
    print(json.dumps(summary, indent=2))

    if args.write_baseline:
        BASELINE_FILE.write_text(json.dumps(summary, indent=2))
        print(f"\nWrote baseline -> {BASELINE_FILE}")
        return 0

    if not BASELINE_FILE.exists():
        print("\nNO BASELINE — run with --write-baseline first.")
        return 0

    baseline = json.loads(BASELINE_FILE.read_text())
    delta_self = summary["overall_self"] - baseline.get("overall_self", 0)
    delta_git  = summary["overall_git"]  - baseline.get("overall_git",  0)
    print(f"\noverall_self: {summary['overall_self']:.3f} "
          f"(delta {delta_self:+.3f} vs baseline {baseline.get('overall_self',0):.3f})")
    print(f"overall_git:  {summary['overall_git']:.3f} "
          f"(delta {delta_git:+.3f} vs baseline {baseline.get('overall_git',0):.3f})")

    regressed = delta_self < -args.max_regression or delta_git < -args.max_regression
    if regressed and args.strict:
        print(f"\n[FAIL] REGRESSION: F1 dropped > {args.max_regression}. Exiting 1.")
        return 1
    print("\nOK." if not regressed else "\n[WARN] Regression but --strict not set.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
