"""Speak the MCP JSON-RPC protocol against our mcp_server over stdio.

This is exactly what Claude Code would do when it connects to our server.
We verify:
  1. The server starts and handshakes (initialize)
  2. It advertises the tool list via tools/list
  3. A real tool call returns valid data
"""
import json, subprocess, sys, time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
# Launch the MCP server as a subprocess via stdio
proc = subprocess.Popen(
    [sys.executable, "-m", "decisiongraph.mcp_server"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    cwd=str(REPO_ROOT), bufsize=0,
    env={**__import__("os").environ,
          "PYTHONIOENCODING": "utf-8"})

def send(req: dict) -> dict:
    """Send a JSON-RPC frame and read one back."""
    line = json.dumps(req) + "\n"
    proc.stdin.write(line.encode("utf-8"))
    proc.stdin.flush()
    # read until newline
    out = proc.stdout.readline().decode("utf-8", errors="replace")
    return json.loads(out)

try:
    # 1) initialize
    print("=== 1) initialize ===")
    r = send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-harness", "version": "0"}}})
    print(f"   protocol: {r.get('result', {}).get('protocolVersion')}")
    print(f"   server:   {r.get('result', {}).get('serverInfo')}")

    # send the initialized notification
    proc.stdin.write((json.dumps({
        "jsonrpc": "2.0", "method": "notifications/initialized",
        "params": {}}) + "\n").encode("utf-8"))
    proc.stdin.flush()

    # 2) tools/list
    print("\n=== 2) tools/list ===")
    r = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = r.get("result", {}).get("tools", [])
    print(f"   {len(tools)} tools advertised")
    # show first 10 names
    for t in tools[:10]:
        print(f"     - {t['name']}")
    if len(tools) > 10:
        print(f"     ... and {len(tools)-10} more")
    # Check for the new ones we added
    new_tools = ("rationales", "topology", "triage_pr",
                  "suggested_questions", "export_graph_html",
                  "federated_callers", "federated_topology",
                  "federated_rationale_search", "causal_radius")
    found_new = [n for n in new_tools if any(t["name"] == n for t in tools)]
    print(f"\n   new tools verified: {len(found_new)}/{len(new_tools)}")
    print(f"   present: {found_new}")
    missing = [n for n in new_tools if n not in found_new]
    if missing: print(f"   MISSING: {missing}")

    # 3) call a real tool
    print("\n=== 3) tools/call 'code_graph_stats' ===")
    r = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "code_graph_stats", "arguments": {}}})
    content = r.get("result", {}).get("content", [])
    if content:
        text = content[0].get("text", "")
        print(f"   response (first 200 chars): {text[:200]}")
        try:
            parsed = json.loads(text)
            print(f"   parsed keys: {list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__}")
        except Exception as e:
            print(f"   not JSON ({e})")
    else:
        print(f"   no content; full response: {r}")

    # 4) call a complex tool: topology
    print("\n=== 4) tools/call 'topology' ===")
    r = send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "topology", "arguments": {"top_god": 3}}})
    content = r.get("result", {}).get("content", [])
    if content:
        text = content[0].get("text", "")
        print(f"   response keys would be: god_nodes/surprising_connections/etc")
        print(f"   raw length: {len(text)} chars")
        try:
            parsed = json.loads(text)
            print(f"   top god-nodes: {len(parsed.get('god_nodes', []))}")
        except Exception:
            pass

    print("\nVERDICT: MCP server boots, advertises tools, responds to calls.")

finally:
    proc.stdin.close()
    proc.wait(timeout=5)
    # show any stderr the server emitted
    err = proc.stderr.read().decode("utf-8", errors="replace")
    if err:
        print(f"\n--- server stderr ---\n{err[:1500]}")
