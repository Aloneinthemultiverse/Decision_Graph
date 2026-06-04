"""Trace where the 66 predicted files come from."""
import subprocess, sqlite3
from decisiongraph import code_graph as cg
DB = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
REPO = r"C:\Users\SUJITN~1\AppData\Local\Temp\flask-bench"
SHA = "a29f88ce6f2f9843bd6fcbbfce1390a2071965d6"
changed = subprocess.run(["git","diff","--name-only",f"{SHA}~1",SHA],
                         cwd=REPO, capture_output=True, text=True).stdout.split()
print("CHANGED:", changed)
for f in changed:
    r = cg.blast_radius(DB, f, max_depth=3, strict=True)
    print(f"\n--- {f} -> {r['total_affected']} files ---")
    print("  callers_by_hop:", {h:len(v) for h,v in r["callers_by_hop"].items()})
    print("  reverse_imports:", len(r["reverse_imports"]), "(LIKE-matched importers)")
    print("  reverse_imports sample:", r["reverse_imports"][:5])
