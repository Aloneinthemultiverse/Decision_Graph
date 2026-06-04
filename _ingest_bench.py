"""Ingest the 5 benchmark repos into our SQLite code graph (no LLM, no server)."""
import os, time
from pathlib import Path
from decisiongraph import code_graph as cg

REPOS = {
    "fastapi":           ("tiangolo/fastapi",            r"C:\Users\SUJITN~1\AppData\Local\Temp\fastapi-bench"),
    "httpx":             ("encode/httpx",                r"C:\Users\SUJITN~1\AppData\Local\Temp\httpx-bench"),
    "express":           ("expressjs/express",           r"C:\Users\SUJITN~1\AppData\Local\Temp\express-bench"),
    "gin":               ("gin-gonic/gin",               r"C:\Users\SUJITN~1\AppData\Local\Temp\gin-bench"),
    "code-review-graph": ("tirth8205/code-review-graph", r"C:\Users\SUJITN~1\AppData\Local\Temp\code-review-graph-bench"),
}

OUT_DIR = Path(r"storage\bench_graphs")
OUT_DIR.mkdir(parents=True, exist_ok=True)

for name, (repo_name, path) in REPOS.items():
    db = str(OUT_DIR / f"{name}.db")
    if os.path.exists(db): os.remove(db)
    t = time.time()
    print(f"\n=== {name} === path={path}")
    s = cg.build_from_repo(db, path, repo_name)
    print(f"  files={s['files']}  symbols={s['symbols']}  calls={s['calls']}  imports={s['imports']}  ({s['elapsed_s']}s)")
