from decisiongraph.code_graph_federated import federated_topology
r = federated_topology(["storage/bench_graphs"], top_god=10)
print(f"dbs searched: {r['dbs_searched']}")
print("cross-repo god-nodes appearing in 2+ repos:")
for g in r["cross_repo_god_nodes"]:
    if len(g["per_workspace"]) >= 2:
        print(f"  {g['leaf']:25} total_callers={g['total_callers']:>4}  "
              f"same_concept={g.get('likely_same_concept')}")
        for ws, qn in g["qualified_names_per_workspace"].items():
            print(f"      {ws:25}  {qn}")
