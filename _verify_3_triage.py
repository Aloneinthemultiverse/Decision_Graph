"""VERIFY #3: head-to-head PR triage on flask fbb6f0bc."""
import subprocess
from decisiongraph import code_graph as cg

sha = "fbb6f0bc4c60a0bada0e03c3480d0ccf30a3c1df"
ch = subprocess.run(
    ["git", "diff", "--name-only", sha + "~1", sha],
    cwd=r"C:\Users\SUJITN~1\AppData\Local\Temp\flask-bench",
    capture_output=True, text=True).stdout.split()
print(f"changed files in commit: {len(ch)}")
for f in ch: print(f"  - {f}")

db = (r"C:\Users\Sujit Narrayan M\Downloads\decisiongraph_v2"
      r"\storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db")
out = cg.triage_pr(db, ch, repo="pallets/flask")
print(f"\n--- OUR triage ---")
print(f"  combined_affected:    {len(out['combined_affected_files'])}")
print(f"  god_nodes_touched:    {out['god_nodes_touched']}")
print(f"  rationale_warnings:   {len(out['rationale_warnings'])}")
print(f"  risk_score:           {out['risk_score']} ({out['risk_tier']})")
print(f"  merge_order_hint:     {out['merge_order_hint']}")

print(f"\n--- THEIR triage (from earlier run) ---")
print(f"  risk_score:           0.00")
print(f"  changed_functions:    0")
print(f"  affected_flows:       0")
