"""Mutation test: break a god-node method and verify our triage_pr scoring.

Mutation: rename `add_url_rule` -> `add_url_rule_v2_BROKEN` in scaffold.py
This is a 45-caller god-node — any sane risk system should flag HIGH.
"""
import subprocess
from pathlib import Path
from decisiongraph import code_graph as cg

REPO = Path(r"C:\Users\SUJITN~1\AppData\Local\Temp\loop_flask")
DB = str(REPO / ".dg_code_graph.db")
TARGET = REPO / "src/flask/sansio/scaffold.py"

# baseline triage (just touching the file, no mutation yet)
print("=== baseline: triage on scaffold.py with no destructive edit ===")
br_base = cg.triage_pr(DB, ["src/flask/sansio/scaffold.py"],
                       repo="pallets/flask", max_hops=3)
print(f"  risk: {br_base['risk_score']} ({br_base['risk_tier']})")
print(f"  god_nodes_touched: {br_base['god_nodes_touched']}")
print(f"  hint: {br_base['merge_order_hint']}")

# also: triage on a non-god-node file for contrast
print("\n=== baseline contrast: triage on a sleepy file ===")
sleepy = "src/flask/json/provider.py"   # something less central
br_sleepy = cg.triage_pr(DB, [sleepy], repo="pallets/flask", max_hops=3)
print(f"  {sleepy}: risk {br_sleepy['risk_score']} ({br_sleepy['risk_tier']})")
print(f"    god_nodes_touched: {br_sleepy['god_nodes_touched']}")

# now apply the mutation
print("\n=== apply mutation: rename add_url_rule -> add_url_rule_v2_BROKEN ===")
src = TARGET.read_text(encoding="utf-8")
mutated = src.replace("def add_url_rule(",
                       "def add_url_rule_v2_BROKEN(")
print(f"  edits: {(src != mutated)}, "
      f"diff_chars: {len(mutated) - len(src)}")
TARGET.write_text(mutated, encoding="utf-8")

# commit (fires hook)
subprocess.run(["git", "add", "src/flask/sansio/scaffold.py"], cwd=REPO,
                check=True, capture_output=True)
r = subprocess.run(
    ["git", "commit", "-m", "MUTATION: rename add_url_rule (will break callers)"],
    cwd=REPO, capture_output=True, text=True)
print(f"  commit exit: {r.returncode}")

# triage the mutation
print("\n=== triage_pr ON the mutation ===")
br_mut = cg.triage_pr(DB, ["src/flask/sansio/scaffold.py"],
                      repo="pallets/flask", max_hops=3)
print(f"  risk: {br_mut['risk_score']} ({br_mut['risk_tier']})")
print(f"  god_nodes_touched: {br_mut['god_nodes_touched']}")
print(f"  hint: {br_mut['merge_order_hint']}")

# verify the graph noticed: original add_url_rule symbol gone, _BROKEN one appears
import sqlite3
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
old = conn.execute(
    "SELECT COUNT(*) n FROM symbols WHERE name='add_url_rule' "
    "AND path='src/flask/sansio/scaffold.py'").fetchone()["n"]
new = conn.execute(
    "SELECT COUNT(*) n FROM symbols WHERE name='add_url_rule_v2_BROKEN'"
).fetchone()["n"]
print(f"\n=== graph state after mutation ===")
print(f"  add_url_rule (in scaffold.py): {old}  (expected 0)")
print(f"  add_url_rule_v2_BROKEN:        {new}  (expected 1)")
conn.close()

# also run pytest to confirm BREAKAGE
print("\n=== run pytest — should FAIL now ===")
r = subprocess.run(["python", "-m", "pytest", "tests/", "--tb=line", "-q",
                    "--maxfail=5"], cwd=REPO, capture_output=True, text=True)
last_lines = (r.stdout + r.stderr).strip().splitlines()[-8:]
for ln in last_lines: print(f"  {ln}")
print(f"  exit code: {r.returncode}  (non-zero = tests failed = mutation caught)")

# Cleanup: revert mutation
print("\n=== revert mutation ===")
TARGET.write_text(src, encoding="utf-8")
subprocess.run(["git", "add", "-A"], cwd=REPO, capture_output=True)
subprocess.run(["git", "commit", "-m", "revert mutation"],
                cwd=REPO, capture_output=True)
print("  done")
