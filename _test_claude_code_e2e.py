"""Final integration test: simulate exactly what Claude Code does.

1. Read .mcp.json (the config Claude Code reads)
2. Spawn the MCP server using those args/env/cwd
3. Speak JSON-RPC: initialize → tools/list → tools/call
4. Verify tools return REAL data from the flask repo (not zeros)
5. Ask one tool to find `mark_dirty` — the symbol we added in the loop test
"""
import json, subprocess, sys, threading, time, os
from queue import Queue, Empty
from pathlib import Path

cfg = json.loads(Path(".mcp.json").read_text(encoding="utf-8"))
srv = cfg["mcpServers"]["decisiongraph"]
print(f"=== launching MCP server per .mcp.json ===")
print(f"   command: {srv['command']} {' '.join(srv['args'])}")
print(f"   cwd:     {srv['cwd']}")
print(f"   db:      {srv['env'].get('DG_CODE_GRAPH_DB', '(default)')}")

env = {**os.environ, **srv["env"], "PYTHONUNBUFFERED": "1"}
proc = subprocess.Popen(
    [srv["command"], "-u"] + srv["args"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    cwd=srv["cwd"], env=env, bufsize=0)

q: Queue = Queue()
def _read():
    for line in iter(proc.stdout.readline, b""):
        q.put(line.decode("utf-8", errors="replace"))
threading.Thread(target=_read, daemon=True).start()

def send(req, wait=240):
    proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
    proc.stdin.flush()
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            line = q.get(timeout=0.5).strip()
            if line.startswith("{"):
                return json.loads(line)
        except Empty: continue
    return None

results = {}
try:
    # 1. initialize
    r = send({"jsonrpc":"2.0","id":1,"method":"initialize",
              "params":{"protocolVersion":"2024-11-05","capabilities":{},
                        "clientInfo":{"name":"e2e","version":"0"}}})
    results["init_ok"] = (r is not None
                          and r.get("result", {}).get("protocolVersion"))
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
    proc.stdin.flush()

    print(f"   init response: {r is not None and 'result' in (r or {})}")
    if r is None:
        raise SystemExit("FAIL: no init response within 90s — check stderr below")

    # 2. tools/list — count
    r = send({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
    if r is None:
        raise SystemExit("FAIL: no tools/list response — check stderr")
    results["tools_count"] = len(r["result"]["tools"])

    # 3. code_graph_stats — should now show REAL flask numbers
    r = send({"jsonrpc":"2.0","id":3,"method":"tools/call",
              "params":{"name":"code_graph_stats","arguments":{}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    results["stats"] = payload

    # 4. find_callers('mark_dirty') — should be the one we added
    r = send({"jsonrpc":"2.0","id":4,"method":"tools/call",
              "params":{"name":"find_callers",
                         "arguments":{"name":"mark_dirty"}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    results["find_mark_dirty"] = payload.get("count", 0)

    # 5. confirm graph has the symbol via blast_radius
    r = send({"jsonrpc":"2.0","id":5,"method":"tools/call",
              "params":{"name":"blast_radius",
                         "arguments":{"path":"mark_dirty","max_depth":2}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    results["blast_anchor"] = payload.get("changed_file")
    results["mark_dirty_in_symbols"] = ("mark_dirty"
        in (payload.get("symbols_in_changed_file") or []))

    # 6. real triage on the file we edited
    r = send({"jsonrpc":"2.0","id":6,"method":"tools/call",
              "params":{"name":"triage_pr",
                         "arguments":{"changed_files":["src/flask/ctx.py"]}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    results["triage_score"] = payload.get("risk_score")
    results["triage_tier"]  = payload.get("risk_tier")
    results["god_nodes_touched"] = payload.get("god_nodes_touched")

finally:
    proc.stdin.close()
    try: proc.wait(timeout=3)
    except Exception: proc.kill()
    # Dump stderr for diagnosis
    err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
    if err:
        Path(".bench/mcp_err.log").write_text(err, encoding="utf-8")
        ascii_tail = err[-2000:].encode("ascii", "replace").decode()
        print(f"\n--- stderr tail (ascii-safe, full saved to .bench/mcp_err.log) ---")
        print(ascii_tail)
    # Drain remaining stdout
    leftover = []
    while True:
        try: leftover.append(q.get_nowait())
        except Empty: break
    if leftover:
        print(f"\n--- leftover stdout ({len(leftover)} lines) ---")
        for l in leftover[:10]: print(f"  {l.rstrip()[:200]}")

print("\n=== RESULTS ===")
for k, v in results.items():
    print(f"  {k}: {v}")

passed = (
    results["init_ok"]
    and results["tools_count"] == 27
    and results["stats"]["files"] > 0
    and results["stats"]["symbols"] > 1000
    and results["mark_dirty_in_symbols"]   # our edit IS visible
    and results["blast_anchor"] == "src/flask/ctx.py"
    and results["triage_tier"] in ("MEDIUM", "HIGH")
)
print(f"\nVERDICT: {'PASS — Claude Code can use this end-to-end' if passed else 'FAIL'}")
