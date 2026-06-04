"""VERIFY #2: HTML actually renders + has valid data."""
import json, re
content = open("graph.html", encoding="utf-8").read()
print(f"size_kb: {len(content)//1024}")
print(f"DOCTYPE present:  {content.startswith('<!doctype html>')}")
print(f"toolbar present:  {'<div id=\"toolbar\">' in content}")
print(f"viz div present:  {'<div id=\"viz\">' in content}")
print(f"vis-network ref:  {'vis-network' in content}")

m = re.search(r"const DATA = (.+?);\nconst nodes", content, re.DOTALL)
data = json.loads(m.group(1))
print(f"nodes embedded:   {len(data['nodes'])}")
print(f"edges embedded:   {len(data['edges'])}")
n = data['nodes'][0]
print(f"sample node:      id={n['id']} label={n['label']} color={n['color']}")
e = data['edges'][0]
print(f"sample edge:      from={e['from']} to={e['to']} bucket={e['bucket']}")

# bucket distribution in the rendered subset
buckets = {}
for e in data["edges"]:
    buckets[e["bucket"]] = buckets.get(e["bucket"], 0) + 1
print(f"edges by bucket:  {buckets}")

# god-node highlights
god_count = sum(1 for n in data["nodes"] if n["color"] == "#f85149")
print(f"god-nodes (red):  {god_count}")

# Now actually launch it in the default browser
import subprocess, os
print("\n--- launching graph.html in default browser via 'start' ---")
path = os.path.abspath("graph.html")
subprocess.Popen(["cmd", "/c", "start", "", path], shell=False)
print(f"launched: {path}")
print("(opens in your default browser — vis-network bundle loaded from CDN once)")
