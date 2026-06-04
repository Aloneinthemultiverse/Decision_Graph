"""Validate Cypher export structurally — every line must be a well-formed
CREATE or MATCH-CREATE, parens/braces balanced, strings escaped."""
import re
from decisiongraph.code_graph_viz import export_cypher

DB = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
out_path = ".bench/cypher_test.cypher"
r = export_cypher(DB, out_path, repo="pallets/flask")
print(f"exported: {r}")

lines = [l for l in open(out_path, encoding="utf-8").read().splitlines()
         if l.strip() and not l.startswith("//")]
print(f"\nstatements: {len(lines)}")

create_re = re.compile(
    r"^CREATE \(s\d+:Symbol \{id:\d+, qualified_name:'[^']*', "
    r"name:'[^']*', path:'[^']*', kind:'[^']*'\}\)$")
edge_re = re.compile(
    r"^MATCH \(a:Symbol \{id:\d+\}\), \(b:Symbol \{id:\d+\}\) "
    r"CREATE \(a\)-\[:CALLS \{bucket:'[^']+'\}\]->\(b\)$")

n_create = n_edge = n_bad = 0
bad_samples = []
known_node_ids = set()
edge_endpoints = []

for line in lines:
    if line.startswith("CREATE (s"):
        if create_re.match(line):
            m = re.search(r"\{id:(\d+),", line)
            if m: known_node_ids.add(int(m.group(1)))
            n_create += 1
        else:
            n_bad += 1; bad_samples.append(line[:150])
    elif line.startswith("MATCH "):
        if edge_re.match(line):
            ids = re.findall(r"\{id:(\d+)\}", line)
            edge_endpoints.extend(int(i) for i in ids)
            n_edge += 1
        else:
            n_bad += 1; bad_samples.append(line[:150])
    else:
        n_bad += 1; bad_samples.append(line[:150])

# Check edges reference existing nodes
missing = [eid for eid in set(edge_endpoints) if eid not in known_node_ids]

# Balanced parens/braces in each statement
unbalanced = []
for line in lines:
    if line.count("(") != line.count(")") or line.count("{") != line.count("}"):
        unbalanced.append(line[:80])

# Stray single quotes (would break Cypher string literals)
stray_quotes = []
for line in lines[:5000]:
    # strip out properly-quoted strings, then count remaining quotes
    stripped = re.sub(r"'[^']*'", "", line)
    if "'" in stripped:
        stray_quotes.append(line[:80])

print(f"  CREATE Symbol stmts:  {n_create:>5}")
print(f"  MATCH-CREATE edges:   {n_edge:>5}")
print(f"  malformed stmts:      {n_bad:>5}")
print(f"  edges -> missing-node: {len(missing)}")
print(f"  unbalanced parens:    {len(unbalanced)}")
print(f"  stray single-quotes:  {len(stray_quotes)}")

if bad_samples:
    print(f"\nbad samples (first 3):")
    for s in bad_samples[:3]: print(f"  {s}")
if missing:
    print(f"\nfirst 3 missing-node ids in edges: {missing[:3]}")
if stray_quotes:
    print(f"\nfirst 3 lines with stray quotes:")
    for s in stray_quotes[:3]: print(f"  {s}")

# Overall verdict
verdict = ("PASS" if (n_bad == 0 and not missing and not unbalanced
                       and not stray_quotes) else "FAIL")
print(f"\nVERDICT: {verdict}")
