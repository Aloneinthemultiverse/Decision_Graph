"""Performance + ordering test for causal_radius."""
import asyncio, time
from decisiongraph.workspace import Workspace
from decisiongraph.mcp_server import _h_causal_radius

ws = Workspace("p4jIEgrAJd33s3qH-DZbhQ",
                "storage/workspaces/p4jIEgrAJd33s3qH-DZbhQ")
dg = ws.dg

# How many decisions exist
before = len(dg.memory.all_decisions())
print(f"existing decisions: {before}")

# Add 200 dummy decisions mentioning ctx.py to test scaling
print("seeding 200 dummy decisions...")
t = time.time()
for i in range(200):
    dg.memory.store(
        question=f"[adr:bulk-{i:03d}] dummy decision {i}",
        answer=f"This is a synthetic decision mentioning src/flask/ctx.py "
               f"at iteration {i}. Year-month tag 2026-05-{i % 28 + 1:02d}.",
        reasoning_summary=f"bulk seed {i}",
        communities_used=[], context_triples=[])
seeded = time.time() - t
print(f"  seeded in {seeded:.2f}s")

# Now query causal_radius and time it
n = len(dg.memory.all_decisions())
print(f"\ntotal decisions now: {n}")

print("calling causal_radius 3x and timing...")
times = []
for _ in range(3):
    t = time.time()
    out = asyncio.run(_h_causal_radius(dg, None, None,
        path="src/flask/ctx.py", repo="pallets/flask"))
    times.append(time.time() - t)
print(f"  per-call times: {[round(x*1000, 1) for x in times]} ms")
print(f"  avg: {sum(times)/len(times)*1000:.1f} ms")

print(f"\nresult counts:")
print(f"  total_affected:                 {out['total_affected']}")
print(f"  related_decisions:              {out['total_related_decisions']}")
print(f"  related_prs:                    {len(out.get('related_prs') or [])}")
print(f"  related_commits:                {len(out.get('related_commits') or [])}")

# Verify ordering: most recent first
rd = out.get("related_decisions") or []
if len(rd) >= 2:
    ts = [r.get("timestamp") for r in rd]
    is_desc = all(ts[i] >= ts[i+1] for i in range(len(ts)-1) if ts[i] and ts[i+1])
    print(f"\nrelated_decisions ordered most-recent-first: {is_desc}")
    print(f"  first 3 timestamps: {ts[:3]}")
