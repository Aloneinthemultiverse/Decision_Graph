from decisiongraph import code_graph as cg
import sqlite3, subprocess
db = 'storage/bench_graphs/gin.db'
conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT confidence, COUNT(*) c FROM calls GROUP BY confidence").fetchall()
print("calls by confidence:")
for r in rows: print(f"  conf={r['confidence']:.2f} -> {r['c']}")
print()
SHA = '5c00df8afadd06cc5be530dde00fe6d9fa4a2e4a'
ch = subprocess.run(['git','diff','--name-only',f'{SHA}~1',SHA],
                    cwd=r'C:\Users\SUJITN~1\AppData\Local\Temp\gin-bench',
                    capture_output=True, text=True).stdout.split()
print(f"changed files ({len(ch)}):", ch[:10])
for f in ch[:3]:
    r = cg.blast_radius(db, f, max_depth=3, strict=True)
    print(f"\n{f} -> total_affected={r['total_affected']}")
    print(f"  hops: { {h:len(v) for h,v in r['callers_by_hop'].items()} }")
    print(f"  reverse_imports: {len(r['reverse_imports'])}")
    print(f"  symbols_in_file ({len(r['symbols_in_changed_file'])}): {r['symbols_in_changed_file'][:5]}")

# Same for actual (their methodology) using OUR graph
import sys; sys.path.insert(0, '.')
from _eval_all import actual_set
for f in ch[:3]:
    a = actual_set(ch, db)
    break
print(f"\nour-graph actual_set size: {len(a)}")
print("their published actual: 23 files (from CSV)")
