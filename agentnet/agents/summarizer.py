"""Sample sandboxed agent (Slice-1 demo).

Contract: read the host-approved scoped slice from /sandbox/input.json and
write ONE JSON object to stdout. Has no network and sees nothing but this
file (enforced by the Step-2 sandbox). Pure stdlib, deterministic.
"""
import json

data = json.load(open("/sandbox/input.json"))
task = data.get("task", "")
scope = data.get("data", {})

lines = []
best = None
for topic, records in scope.items():
    lines.append(f"- {topic}: {len(records)} record(s)")
    for r in records:
        if best is None or r.get("confidence", 0) > best.get("confidence", 0):
            best = {"topic": topic, **r}

summary = f"Task: {task}\nScope reviewed:\n" + "\n".join(lines)
if best:
    summary += (f"\nHighest-confidence item ({best['topic']}, "
                f"conf={best.get('confidence')}): "
                f"{str(best.get('answer',''))[:300]}")

print(json.dumps({
    "summary": summary,
    "answer": summary,
    "topics_reviewed": list(scope.keys()),
    "records_seen": sum(len(v) for v in scope.values()),
}))
