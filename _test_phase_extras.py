"""Test the new Phase-1.5 / Phase-3 additions:
  1. Rationale extraction
  2. Topology (god-nodes, surprising-connections)
  3. Suggested questions
  5. PR triage scoring
"""
from decisiongraph import code_graph as cg
import json

DB   = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
REPO = "pallets/flask"
PATH = r"C:\Users\SUJITN~1\AppData\Local\Temp\flask-bench"

# Rebuild flask with the new extractors
print("=== rebuild flask ===")
s = cg.build_from_repo(DB, PATH, REPO)
print(f"files={s['files']} symbols={s['symbols']} calls={s['calls']} "
      f"imports={s['imports']} inherits={s['inherits']} "
      f"rationales={s['rationales']}  ({s['elapsed_s']}s)")

print("\n=== #1 rationales (top 8) ===")
import sqlite3
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT tag, path, line, text FROM rationales WHERE repo=? "
    "ORDER BY tag, line LIMIT 8", (REPO,)).fetchall()
for r in rows:
    print(f"  [{r['tag']:8}] {r['path']}:{r['line']} -> {r['text'][:80]}")

print("\n=== #2 topology ===")
top = cg.analyze_topology(DB, repo=REPO, top_god=5, top_surprise=3)
print(" god_nodes (top 5):")
for g in top["god_nodes"]:
    print(f"   {g['caller_count']:>3} callers  {g['qualified_name']}  @ {g['path']}")
print(" surprising_connections (top 3):")
for s2 in top["surprising_connections"]:
    print(f"   {s2['src_folder']} -> {s2['dst_folder']}  (only {s2['edge_count']} edges)")
print(f" orphans: {len(top['orphans'])} symbols")
print(f" deep inheritance: {len(top['deep_inheritance_chains'])} chains")

print("\n=== #3 suggested questions ===")
qs = cg.suggested_questions(DB, repo=REPO)
for i, q in enumerate(qs, 1): print(f"  {i}. {q}")

print("\n=== #5 PR triage scoring ===")
# simulate a PR touching ctx.py + helpers.py
pr_files = ["src/flask/ctx.py", "src/flask/helpers.py", "src/flask/app.py"]
out = cg.triage_pr(DB, pr_files, repo=REPO, max_hops=3)
print(f" changed_files: {out['changed_files']}")
print(f" combined_affected_files: {len(out['combined_affected_files'])}")
print(f" god_nodes_touched: {out['god_nodes_touched']}")
print(f" rationale_warnings: {len(out['rationale_warnings'])}")
print(f" RISK SCORE: {out['risk_score']} ({out['risk_tier']})")
print(f" merge_order_hint: {out['merge_order_hint']}")

print("\n=== #4 CLI / hook smoke test ===")
import subprocess, sys
r = subprocess.run([sys.executable, "-m", "decisiongraph.code_graph_cli",
                    "rebuild", "--repo-path", PATH, "--repo-name", REPO,
                    "--db", "_test_cli.db"],
                   capture_output=True, text=True)
print(r.stdout.strip())
print(r.stderr.strip())
import os
if os.path.exists("_test_cli.db"): os.remove("_test_cli.db")
