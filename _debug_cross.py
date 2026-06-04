import subprocess
from code_review_graph.graph import GraphStore
from decisiongraph import code_graph as cg

REPO = r"C:\Users\SUJITN~1\AppData\Local\Temp\flask-bench"
OUR_DB = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
THEIR_DB = REPO + r"\.code-review-graph\graph.db"
SHA = "a29f88ce6f2f9843bd6fcbbfce1390a2071965d6"

changed = subprocess.run(["git","diff","--name-only",f"{SHA}~1",SHA],
                         cwd=REPO, capture_output=True, text=True).stdout.split()
print("changed:", changed)

# Their actual (mirrors impact_accuracy.py)
store = GraphStore(THEIR_DB)
theirs = set(changed)
for f in changed:
    for node in store.get_nodes_by_file(f):
        for edge in store.get_edges_by_target(node.qualified_name):
            if edge.kind in ("CALLS", "IMPORTS_FROM"):
                src_file = edge.source_qualified.split("::")[0] if "::" in edge.source_qualified else ""
                if src_file: theirs.add(src_file)

# Ours
ours = set(changed)
breakdown_calls = set()
breakdown_imports = set()
for f in changed:
    r = cg.blast_radius(OUR_DB, f, max_depth=1, strict=True)
    ours.update(r["affected_files"])
    for paths in r["callers_by_hop"].values():
        breakdown_calls.update(paths)
    breakdown_imports.update(r["reverse_imports"])

print(f"\ntheirs ({len(theirs)}):", sorted(theirs))
print(f"\nours ({len(ours)}):", sorted(ours))
print(f"\noverlap ({len(theirs & ours)}):", sorted(theirs & ours))
print(f"\nin OURS but not theirs ({len(ours - theirs)}):", sorted(ours - theirs)[:20])
print(f"  └─ via calls: {len(breakdown_calls - theirs)}")
print(f"  └─ via imports: {len(breakdown_imports - theirs)}")
