from decisiongraph import code_graph as cg
import sqlite3
# Try the larger codebases for richer rationale data
for name, path, repo in [
    ("crg",     r"C:\Users\SUJITN~1\AppData\Local\Temp\code-review-graph-bench", "tirth8205/code-review-graph"),
    ("fastapi", r"C:\Users\SUJITN~1\AppData\Local\Temp\fastapi-bench", "tiangolo/fastapi"),
    ("gin",     r"C:\Users\SUJITN~1\AppData\Local\Temp\gin-bench", "gin-gonic/gin"),
]:
    db = f"storage/bench_graphs/{name}.db"
    s = cg.build_from_repo(db, path, repo)
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    print(f"\n[{name}] rationales={s['rationales']}")
    for r in conn.execute("SELECT tag, COUNT(*) c FROM rationales GROUP BY tag ORDER BY c DESC").fetchall():
        print(f"  {r['tag']:10} {r['c']}")
    sample = conn.execute("SELECT tag, path, line, text FROM rationales WHERE tag IN ('HACK','SAFETY','BUG','FIXME','WARNING') LIMIT 3").fetchall()
    for r in sample:
        print(f"  -> [{r['tag']}] {r['path']}:{r['line']}  {r['text'][:80]}")
    conn.close()
