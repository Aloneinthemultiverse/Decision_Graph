import time
from decisiongraph import code_graph as cg
db = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
print("STATS:", cg.stats(db))
for sym in ["add_url_rule", "route", "dispatch_request", "full_dispatch_request"]:
    t = time.perf_counter()
    r = cg.find_callers(db, sym)
    print(f"find_callers({sym}): {(time.perf_counter()-t)*1000:.2f} ms -> {len(r)} rows")
    t = time.perf_counter()
    r = cg.blast_radius(db, sym, max_depth=3)
    n = len(r.get("affected_files", [])) if isinstance(r, dict) else len(r)
    tot = r.get("total_affected") if isinstance(r, dict) else "-"
    print(f"blast_radius({sym},3): {(time.perf_counter()-t)*1000:.2f} ms -> {n} files, total_affected={tot}")
