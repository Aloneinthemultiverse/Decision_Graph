"""Test incremental update_files: structural accuracy + major/minor detection."""
import os
from decisiongraph import code_graph as cg

DB = ".bench/update_test.db"
if os.path.exists(DB): os.remove(DB)

# Build a tiny repo
import tempfile, pathlib
repo = pathlib.Path(tempfile.mkdtemp())
(repo / "util.py").write_text("def helper(x):\n    return x + 1\n", encoding="utf-8")
(repo / "main.py").write_text(
    "from util import helper\n\n"
    "def run():\n    return helper(5)\n", encoding="utf-8")
cg.build_from_repo(DB, str(repo), "test/upd")
print("baseline:", cg.stats(DB, repo="test/upd"))
print("callers of helper:", [c['caller'] for c in cg.find_callers(DB, 'helper', repo='test/upd')])

# --- MINOR edit: change helper's body, no new symbols ---
print("\n=== MINOR edit (body only) ===")
r = cg.update_files(DB, [{
    "path": "util.py",
    "text": "def helper(x):\n    # tweak\n    return x + 2\n"
}], repo="test/upd")
print(f"  major={r['per_file']['util.py']['major']} reason={r['per_file']['util.py']['reason']}")
print(f"  elapsed={r['elapsed_s']}s")
assert r['per_file']['util.py']['major'] is False, "body-only should be MINOR"

# --- MAJOR edit: add a new function ---
print("\n=== MAJOR edit (new function added) ===")
r = cg.update_files(DB, [{
    "path": "util.py",
    "text": "def helper(x):\n    return x + 2\n\n"
            "def new_validator(y):\n    return y > 0\n"
}], repo="test/upd")
pf = r['per_file']['util.py']
print(f"  major={pf['major']} reason={pf['reason']} added={pf['added']}")
print(f"  semantic_refresh_needed={r['semantic_refresh_needed']}")
assert pf['major'] is True, "new symbol should be MAJOR"
assert 'new_validator' in pf['added']

# verify the new symbol is queryable structurally
import sqlite3
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
n = conn.execute("SELECT COUNT(*) c FROM symbols WHERE name='new_validator'").fetchone()['c']
print(f"  new_validator in symbols: {n}")
assert n == 1
conn.close()

# --- check semantic_stale list ---
print("\n=== semantic_stale ===")
stale = cg.list_semantic_stale(DB, repo="test/upd")
print(f"  {len(stale)} stale files:")
for s in stale: print(f"    {s['path']}  reason={s['reason']}")

# --- clear stale (simulate semantic refresh done) ---
cleared = cg.clear_semantic_stale(DB, ["util.py"], repo="test/upd")
print(f"\n  cleared {cleared} stale flags")
assert len(cg.list_semantic_stale(DB, repo="test/upd")) == 0

# --- MAJOR edit: remove a function ---
print("\n=== MAJOR edit (function removed) ===")
r = cg.update_files(DB, [{
    "path": "util.py",
    "text": "def helper(x):\n    return x + 2\n"   # new_validator gone
}], repo="test/upd")
pf = r['per_file']['util.py']
print(f"  major={pf['major']} reason={pf['reason']} removed={pf['removed']}")
assert pf['major'] is True and 'new_validator' in pf['removed']

print("\nALL ASSERTIONS PASSED")
