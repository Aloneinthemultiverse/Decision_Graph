from decisiongraph import code_graph as cg
db = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
import sqlite3
c = sqlite3.connect(db); c.row_factory = sqlite3.Row
rows = c.execute("SELECT repo, path, name, qualified_name FROM symbols WHERE name='add_url_rule'").fetchall()
print(f"symbols matching add_url_rule: {len(rows)}")
for r in rows[:5]: print(" ", dict(r))
print()
r = cg.blast_radius(db, "add_url_rule", max_depth=3)
print("result keys:", list(r.keys()))
print("changed_file:", r["changed_file"])
print("symbols_in_changed_file:", r["symbols_in_changed_file"][:10])
print("callers_by_hop:", r["callers_by_hop"])
print("total_affected:", r["total_affected"])
print("affected_files:", r["affected_files"][:10])
