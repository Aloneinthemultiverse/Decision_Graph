import sqlite3
DB = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
sym = conn.execute(
    "SELECT id FROM symbols WHERE name='update' AND path='examples/tutorial/flaskr/blog.py'").fetchone()
print(f"sid: {sym['id']}")
rows = conn.execute(
    "SELECT s.path AS src_path, s.qualified_name AS src, c.confidence "
    "FROM calls c JOIN symbols s ON c.src_id=s.id WHERE c.dst_id=? LIMIT 20",
    (sym['id'],)).fetchall()
print(f"{len(rows)} callers (sample):")
for r in rows:
    print(f"  conf={r['confidence']} {r['src_path']:50} {r['src']}")
# by confidence
cnt = conn.execute(
    "SELECT confidence, COUNT(*) c FROM calls WHERE dst_id=? GROUP BY confidence",
    (sym['id'],)).fetchall()
print("\nby confidence:")
for r in cnt: print(f"  conf={r['confidence']}: {r['c']}")
