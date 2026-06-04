import sqlite3, subprocess
from decisiongraph import code_graph as cg
db = 'storage/bench_graphs/fastapi.db'
conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
print("=== resolution breakdown ===")
for r in conn.execute("SELECT confidence, COUNT(*) c FROM calls GROUP BY confidence").fetchall():
    print(f"  conf={r['confidence']:.2f} -> {r['c']}")

SHA = '0227991a01e61bf5cdd93cc00e9e243f52b47a4a'
ch = subprocess.run(['git','diff','--name-only',f'{SHA}~1',SHA],
                    cwd=r'C:\Users\SUJITN~1\AppData\Local\Temp\fastapi-bench',
                    capture_output=True, text=True).stdout.split()
print(f"\nchanged files ({len(ch)}): {ch[:10]}")
for f in ch[:5]:
    syms = conn.execute("SELECT id, name FROM symbols WHERE path=?", (f,)).fetchall()
    print(f"\n{f}: {len(syms)} symbols")
    if syms:
        # how many calls target these symbols at any confidence?
        ids = [s['id'] for s in syms]
        ph = ",".join('?' for _ in ids)
        cnt = conn.execute(f"SELECT COUNT(*) FROM calls WHERE dst_id IN ({ph})", ids).fetchone()[0]
        hi  = conn.execute(f"SELECT COUNT(*) FROM calls WHERE dst_id IN ({ph}) AND confidence >= 0.5", ids).fetchone()[0]
        print(f"  total callers (any conf): {cnt}")
        print(f"  high-conf callers (>=0.5): {hi}")
        # Where do those callers live?
        caller_paths = conn.execute(
            f"SELECT DISTINCT s.path FROM calls c JOIN symbols s ON c.src_id=s.id "
            f"WHERE c.dst_id IN ({ph}) AND c.confidence >= 0.5", ids).fetchall()
        print(f"  caller paths ({len(caller_paths)}):", [r['path'] for r in caller_paths[:10]])
    r = cg.blast_radius(db, f, max_depth=3, strict=True)
    print(f"  blast_radius -> {r['total_affected']} files; rev_imports={len(r['reverse_imports'])}")
