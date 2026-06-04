"""Test rationale edge cases on synthetic fixtures."""
from decisiongraph.codebase_ast import parse_file_full

cases = [
    (".bench/rat_fixtures/edge_py.py",
     ["WHY", "NOTE", "HACK", "SAFETY", "BUG", "FIXME", "PERF", "WARNING", "DEPRECATED"]),
    (".bench/rat_fixtures/edge_js.js",
     ["WHY", "NOTE", "HACK", "TODO", "FIXME", "SAFETY", "WARNING"]),
    (".bench/rat_fixtures/edge_go.go",
     ["WHY", "NOTE", "HACK", "SAFETY", "BUG", "DEPRECATED"]),
]

all_pass = True
for path, expected_tags in cases:
    code = open(path, encoding="utf-8").read()
    r = parse_file_full(path, code)
    rats = r.get("rationales") or []
    found = {x["tag"] for x in rats}
    missing = [t for t in expected_tags if t not in found]
    extra = found - set(expected_tags)
    status = "OK" if not missing else "FAIL"
    if missing: all_pass = False
    print(f"\n[{status}] {path}")
    print(f"  expected: {expected_tags}")
    print(f"  found:    {sorted(found)}")
    if missing: print(f"  MISSING:  {missing}")
    if extra:   print(f"  unexpected: {sorted(extra)}")
    print(f"  details:")
    for x in rats:
        safe = x['text'][:60].encode("ascii", "replace").decode()
        print(f"    [{x['tag']:10}] line {x['line']:>3}  {safe}")

print(f"\n{'='*60}\nOVERALL: {'PASS' if all_pass else 'FAIL'}")
