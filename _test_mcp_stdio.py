"""Real stdio MCP JSON-RPC test — this is exactly what Claude Code does."""
import json, subprocess, sys, os, threading, time
from queue import Queue, Empty

env = {**os.environ,
        "PYTHONPATH": r"C:/Users/Sujit Narrayan M/Downloads/decisiongraph_v2",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1"}
proc = subprocess.Popen(
    [sys.executable, "-u", "-m", "decisiongraph.mcp_server"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    cwd=r"C:/Users/Sujit Narrayan M/Downloads/decisiongraph_v2",
    env=env, bufsize=0)

stdout_q: Queue = Queue()
def _read_stdout():
    try:
        for line in iter(proc.stdout.readline, b""):
            stdout_q.put(line.decode("utf-8", errors="replace"))
    except Exception: pass
threading.Thread(target=_read_stdout, daemon=True).start()

# Drain stderr in the background
stderr_buf = []
def _read_stderr():
    try:
        for line in iter(proc.stderr.readline, b""):
            stderr_buf.append(line.decode("utf-8", errors="replace"))
    except Exception: pass
threading.Thread(target=_read_stderr, daemon=True).start()

def send(req, wait=30):
    """Send request, wait up to N seconds for response."""
    proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
    proc.stdin.flush()
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            line = stdout_q.get(timeout=0.5)
            if line.strip().startswith("{"):
                return json.loads(line)
        except Empty:
            continue
    return None

try:
    # 1) initialize
    print("Sending initialize... (DG embed model loads — this takes ~10s)")
    r = send({"jsonrpc":"2.0","id":1,"method":"initialize",
              "params":{"protocolVersion":"2024-11-05",
                        "capabilities":{},
                        "clientInfo":{"name":"test","version":"0"}}}, wait=90)
    if r is None:
        print("FAIL: no initialize response within 90s")
        print("--- stderr tail ---")
        print("".join(stderr_buf)[-1000:])
        sys.exit(1)
    print(f"OK initialize: protocol={r['result'].get('protocolVersion')}")

    # initialized notification
    proc.stdin.write((json.dumps({"jsonrpc":"2.0","method":"notifications/initialized",
                                   "params":{}}) + "\n").encode("utf-8"))
    proc.stdin.flush()

    # 2) tools/list
    print("\nSending tools/list...")
    r = send({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}, wait=30)
    tools = r["result"]["tools"]
    print(f"OK tools/list: {len(tools)} tools")
    for t in tools[:5]:
        print(f"  - {t['name']}: {t.get('description','')[:60]}")
    print(f"  ... and {len(tools)-5} more")

    # 3) call a tool
    print("\nSending tools/call code_graph_stats...")
    r = send({"jsonrpc":"2.0","id":3,"method":"tools/call",
              "params":{"name":"code_graph_stats","arguments":{}}}, wait=30)
    content = r["result"]["content"]
    print(f"OK tools/call: response = {content[0]['text'][:300]}")

    print("\n[PASS] MCP stdio server is reachable by any Claude-Code-style client")
finally:
    try:
        proc.stdin.close()
        proc.wait(timeout=3)
    except Exception:
        proc.kill()
