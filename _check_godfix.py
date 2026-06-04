from decisiongraph import code_graph as cg
DB = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
t = cg.analyze_topology(DB, repo="pallets/flask", top_god=10, top_surprise=0)
print("Top god-nodes after fix:")
for g in t["god_nodes"]:
    print(f"  {g['caller_count']:>4}  {g['qualified_name']:50}  @ {g['path']}")
