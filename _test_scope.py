"""Test the import-scope resolver on synthetic fixtures."""
import os, sqlite3
from decisiongraph import code_graph as cg

DB = ".bench/scope_test.db"
if os.path.exists(DB): os.remove(DB)
s = cg.build_from_repo(DB, ".bench/import_fix", "test/scope")
print(f"ingest: {s['resolution_breakdown']}")

conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
print("\nimports captured per file:")
for r in conn.execute(
    "SELECT src_path, local_name, target_module, target_name, kind "
    "FROM imports").fetchall():
    print(f"  {r['src_path']:25} local={r['local_name']:18} "
          f"target_mod={r['target_module']:25} target_name={r['target_name']}  "
          f"kind={r['kind']}")

print("\ncall resolution per case:")
for caller in ("case_a", "case_b", "case_c", "case_d", "case_relative"):
    rows = conn.execute(
        "SELECT c.dst_name, c.confidence, t.qualified_name AS dst_qn, t.path "
        "FROM calls c JOIN symbols s ON c.src_id=s.id "
        "LEFT JOIN symbols t ON c.dst_id=t.id "
        "WHERE s.name=?", (caller,)).fetchall()
    print(f"  {caller}:")
    for r in rows:
        print(f"    -> {r['dst_name']:20} conf={r['confidence']}  "
              f"resolved={r['dst_qn']}  in={r['path']}")
conn.close()
