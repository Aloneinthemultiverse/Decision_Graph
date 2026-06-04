"""Inspect why test_session_vary_cookie.get is #2 god-node."""
import sqlite3
DB = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row

# Look at the symbol's dst_id callers
sym = conn.execute("SELECT * FROM symbols WHERE name='get' AND path LIKE 'tests/test_basic%'").fetchall()
print(f"symbols named 'get' in tests/test_basic*: {len(sym)}")
for r in sym[:3]: print(f"  id={r['id']} qn={r['qualified_name']} path={r['path']}")

if sym:
    sid = sym[0]["id"]
    # How many calls have dst_id pointing here?
    n = conn.execute("SELECT COUNT(*) FROM calls WHERE dst_id=?", (sid,)).fetchone()[0]
    print(f"\ncalls with dst_id={sid}: {n}")
    # Sample some calls
    rows = conn.execute(
        "SELECT s.path AS src_path, s.qualified_name AS src_name, c.confidence "
        "FROM calls c JOIN symbols s ON c.src_id=s.id WHERE c.dst_id=? LIMIT 10",
        (sid,)).fetchall()
    print("sample callers:")
    for r in rows: print(f"  conf={r['confidence']} {r['src_path']:50} {r['src_name']}")

# Also check how many DIFFERENT symbols share leaf-name 'get'
n_get = conn.execute("SELECT COUNT(*) FROM symbols WHERE name='get'").fetchone()[0]
print(f"\ntotal symbols with leaf-name 'get': {n_get}")
print("breakdown by path (top 5):")
for r in conn.execute(
    "SELECT path, COUNT(*) c FROM symbols WHERE name='get' "
    "GROUP BY path ORDER BY c DESC LIMIT 5").fetchall():
    print(f"  {r['path']:60} {r['c']} 'get' defs")
