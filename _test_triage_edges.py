"""Test triage_pr edge cases."""
from decisiongraph import code_graph as cg

DB = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
REPO = "pallets/flask"

# 1. Empty list
print("[1] empty changed_files:")
r = cg.triage_pr(DB, [], repo=REPO)
print(f"   risk_score={r.get('risk_score')}  tier={r.get('risk_tier')}")

# 2. File that doesn't exist in the graph
print("\n[2] non-existent file:")
r = cg.triage_pr(DB, ["src/flask/nonexistent_xyz.py"], repo=REPO)
print(f"   risk_score={r.get('risk_score')}  tier={r.get('risk_tier')}")
print(f"   combined_affected={len(r.get('combined_affected_files', []))}")
print(f"   per_file: {list(r.get('per_file', {}).keys())}")

# 3. Mix of real + fake
print("\n[3] mix of real + fake:")
r = cg.triage_pr(DB, ["src/flask/ctx.py", "src/flask/nonexistent_xyz.py"], repo=REPO)
print(f"   risk_score={r.get('risk_score')}  tier={r.get('risk_tier')}")
print(f"   combined_affected={len(r.get('combined_affected_files', []))}")

# 4. Massive file list (50 files)
import sqlite3
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
files = [r["path"] for r in conn.execute(
    "SELECT DISTINCT path FROM files LIMIT 50").fetchall()]
conn.close()
print(f"\n[4] {len(files)} files:")
r = cg.triage_pr(DB, files, repo=REPO)
print(f"   risk_score={r.get('risk_score')}  tier={r.get('risk_tier')}")
print(f"   god_nodes_touched={len(r.get('god_nodes_touched', []))}")
print(f"   merge_hint={r.get('merge_order_hint')}")

# 5. None / weird input
print("\n[5] None input (should error gracefully):")
try:
    r = cg.triage_pr(DB, None, repo=REPO)
    print(f"   handled: {r}")
except Exception as e:
    print(f"   raised: {type(e).__name__}: {e}")

# 6. Integration: file WITH a HACK rationale fixture
import os
os.makedirs(".bench/triage_fixture", exist_ok=True)
hack_file = ".bench/triage_fixture/with_hack.py"
with open(hack_file, "w", encoding="utf-8") as f:
    f.write(
        "def critical_path():\n"
        "    # HACK: don't change this without coordinating with PaymentTeam\n"
        "    # SAFETY: this protects against a race condition discovered in INC-481\n"
        "    return True\n")
print(f"\n[6] integration test — ingest a file with HACK + SAFETY rationales:")
test_db = ".bench/triage_test.db"
if os.path.exists(test_db): os.remove(test_db)
s = cg.build_from_repo(test_db, ".bench/triage_fixture", "test/triage")
print(f"   ingested: rationales={s['rationales']}")
tr = cg.triage_pr(test_db, ["with_hack.py"], repo="test/triage")
print(f"   risk_score={tr['risk_score']}  tier={tr['risk_tier']}")
print(f"   rationale_warnings: {len(tr['rationale_warnings'])}")
for w in tr["rationale_warnings"]:
    print(f"     [{w['tag']}] {w['text'][:70]}")
