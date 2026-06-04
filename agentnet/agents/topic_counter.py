"""Sample sandboxed agent: per-topic coverage counts + avg confidence."""
import json

data = json.load(open("/sandbox/input.json"))
task = data.get("task", "")
scope = data.get("data", {})

lines = []
total = 0
for topic, records in scope.items():
    n = len(records)
    total += n
    if n:
        avg = sum(r.get("confidence", 0) for r in records) / n
    else:
        avg = 0.0
    lines.append(f"- {topic}: {n} record(s), avg confidence {avg:.2f}")

summary = (f"Task: {task}\nCoverage:\n" + "\n".join(lines)
           + f"\nTotal records reviewed: {total}")

print(json.dumps({
    "summary": summary,
    "answer": summary,
    "per_topic": {t: len(r) for t, r in scope.items()},
    "total": total,
}))
