"""VERIFY #4: federation across 6 actually-different repos."""
from decisiongraph.code_graph_federated import (
    _resolve_dbs, federated_find_callers, federated_topology,
    federated_rationales_search)

ROOTS = ["storage/bench_graphs"]
dbs = _resolve_dbs(ROOTS)
print(f"DBs found ({len(dbs)}):")
for path, label in dbs:
    print(f"  {label:20} {path}")

# 1) Search a symbol likely to appear in MULTIPLE repos
print("\n--- federated callers of 'route' ---")
r = federated_find_callers(ROOTS, "route", limit_per_db=5)
print(f"dbs_searched={r['dbs_searched']}  total_callers={r['total_callers']}")
for ws, info in r["by_workspace"].items():
    if info.get("count"):
        print(f"  {ws:25} {info['count']} callers")
        for c in info["callers"][:2]:
            print(f"    -> {c['caller']:50} bucket={c['bucket']}")

# 2) Cross-repo god-nodes
print("\n--- cross-repo god-nodes ---")
t = federated_topology(ROOTS, top_god=10)
print(f"dbs_searched={t['dbs_searched']}")
print("god-nodes appearing in MULTIPLE repos (the real value):")
for g in t["cross_repo_god_nodes"][:15]:
    n_repos = len(g["per_workspace"])
    if n_repos >= 2:
        repos = list(g["per_workspace"].keys())
        print(f"  {g['leaf']:25} total={g['total_callers']:>4}  "
              f"in {n_repos} repos: {repos}")

# 3) Cross-repo rationale search
print("\n--- federated rationale search: 'TODO' ---")
fr = federated_rationales_search(ROOTS, "", tag="TODO")
print(f"total TODO hits across all repos: {len(fr['matches'])}")
by_ws = {}
for m in fr["matches"]:
    by_ws[m["workspace"]] = by_ws.get(m["workspace"], 0) + 1
for ws, n in sorted(by_ws.items(), key=lambda x: -x[1]):
    print(f"  {ws:25} {n} TODOs")
