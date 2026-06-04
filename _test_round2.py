"""Smoke-test the new round of features: 6, 7, 8 + multimodal."""
import os
from decisiongraph import code_graph as cg
from decisiongraph.code_graph_viz import export_html, export_cypher
from decisiongraph.code_graph_federated import (
    federated_find_callers, federated_topology, federated_rationales_search)
from decisiongraph.codebase_ast import (
    extract_text_from_any, supported_multimodal_exts)

DB   = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
REPO = "pallets/flask"

print("=== #8 confidence buckets ===")
s = cg.stats(DB, repo=REPO)
print(f"  symbols={s['symbols']}  calls={s['calls']}  rationales={s['rationales']}")
print(f"  edge_buckets: {s['edge_buckets']}")
fc = cg.find_callers(DB, "add_url_rule", repo=REPO, limit=3)
for r in fc:
    print(f"  caller: {r['caller'][:40]}  bucket={r['bucket']}  conf={r['confidence']:.2f}")

print("\n=== #7 HTML viz export ===")
r = export_html(DB, "graph.html", repo=REPO, max_nodes=300)
print(f"  -> {r['path']}  ({r['nodes']} nodes, {r['edges']} edges, {r['size_kb']} KB)")
r2 = export_cypher(DB, "graph.cypher", repo=REPO)
print(f"  -> {r2['path']}  ({r2['nodes']} nodes, {r2['edges']} edges)")

print("\n=== #7 focused HTML (ctx.py only) ===")
r3 = export_html(DB, "graph_ctx.html", repo=REPO,
                  focus_path="src/flask/ctx.py", max_nodes=200)
print(f"  -> {r3['path']}  ({r3['nodes']} nodes, {r3['edges']} edges)")

print("\n=== #6 federated queries ===")
roots = ["storage/workspaces", "storage/bench_graphs"]
fc2 = federated_find_callers(roots, "render", limit_per_db=5)
print(f"  symbol 'render' searched across {fc2['dbs_searched']} dbs, "
      f"total_callers={fc2['total_callers']}")
for ws, info in list(fc2["by_workspace"].items())[:4]:
    n = info.get("count", 0)
    if n: print(f"    {ws}: {n} callers")

ft = federated_topology(roots, top_god=5)
print(f"\n  cross-repo god-nodes (top 5):")
for g in ft["cross_repo_god_nodes"][:5]:
    workspaces = ",".join(g["per_workspace"].keys())
    print(f"    {g['leaf']:30} total_callers={g['total_callers']:>4}  "
          f"({workspaces})")

fr = federated_rationales_search(roots, "deprecated", tag=None)
print(f"\n  rationale 'deprecated' across all repos: {len(fr['matches'])} hits")
for m in fr["matches"][:3]:
    print(f"    [{m['tag']}] {m['workspace']}/{m['path']}:{m['line']}  "
          f"{m['text'][:60]}")

print("\n=== multimodal-lite ===")
print(f"  supported exts: {supported_multimodal_exts()}")
# create a tiny test PDF if pypdf is installed; else just check the API works
test_puml = "_test.puml"
open(test_puml, "w").write(
    "@startuml\nactor User\nUser -> Auth: login\nAuth -> DB: query\n@enduml\n")
r = extract_text_from_any(test_puml)
print(f"  puml extract: kind={r['kind']}  text_len={len(r['text'])}")
os.remove(test_puml)

# cleanup
for f in ("graph.html", "graph.cypher", "graph_ctx.html"):
    if os.path.exists(f):
        size = os.path.getsize(f) // 1024
        print(f"\n  artifact {f}: {size} KB")
