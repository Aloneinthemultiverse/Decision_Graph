"""See what's actually in the workspace's decision memory."""
from decisiongraph.workspace import Workspace
ws = Workspace("p4jIEgrAJd33s3qH-DZbhQ",
                "storage/workspaces/p4jIEgrAJd33s3qH-DZbhQ")
dg = ws.dg
rows = dg.memory.all_decisions()
print(f"total decisions: {len(rows)}")
print("\nAll decision QUESTIONS:")
for i, r in enumerate(rows[:20]):
    q = (r.get("question") or "")[:90]
    print(f"  {i:>2}. {q}")
print("\nany containing 'ctx.py':")
hits = [r for r in rows if "ctx.py" in (r.get("question","") + " " + r.get("answer","")).lower()]
print(f"  → {len(hits)} hits")
for r in hits[:3]:
    print(f"  q: {r['question'][:80]}")
    print(f"  a: {r['answer'][:150]}")
