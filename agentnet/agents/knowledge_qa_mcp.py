"""MCP-speaking agent — uses the `ask` MCP tool.

The gateway exposes `ask(topic, question)` which runs the host-side ReAct
loop on the company's DecisionGraph using the company's LLM gateway. The
sandboxed agent has no LLM access of its own — it just calls the tool over
JSON-RPC stdio and forwards the answer.

This makes the agent universal: any MCP-capable client can plug in and get
a graph-grounded, multi-paragraph, LLM-reasoned answer through one tool call.
"""
import json
import sys

try:
    JOB = json.load(open("/sandbox/task.json"))
except Exception:
    JOB = {"task": "", "topics": []}


def rpc(method, params=None, _id=[0]):
    _id[0] += 1
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0", "id": _id[0], "method": method,
        "params": params or {}}) + "\n")
    sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue


def tool_call(name, args):
    return rpc("tools/call", {"name": name, "arguments": args})


def main():
    task = JOB.get("task", "") or "Summarize what is known in scope."
    topics = JOB.get("topics", []) or []

    rpc("initialize")
    rpc("tools/list")

    if not topics:
        tool_call("done", {"answer": "No topics in scope.", "citation": "n/a"})
        return

    # one MCP call: ask the host-side ReAct agent the question, scoped to the
    # first allowed topic. (Multi-topic ReAct loops can be added later.)
    topic = topics[0]
    r = tool_call("ask", {"topic": topic, "question": task})

    if "error" in r:
        # graceful fallback: if `ask` (host-side ReAct) isn't available,
        # use scoped_search and return the top triple verbatim.
        s = tool_call("scoped_search",
                      {"topic": topic, "query": task, "k": 5})
        try:
            hits = json.loads(s["result"]["content"][0]["text"]).get("hits", [])
        except Exception:
            hits = []
        if not hits:
            tool_call("done", {"answer": "No evidence in scope.",
                                "citation": "n/a"})
            return
        h = hits[0]
        if h.get("kind") == "triple":
            ans = f"{h['subject']} {h['relation']} {h['object']}"
        else:
            ans = h.get("answer") or h.get("summary") or ""
        tool_call("done", {"answer": ans,
                            "citation": "scoped_search (fallback: ask tool unavailable)"})
        return

    try:
        payload = json.loads(r["result"]["content"][0]["text"])
        answer = payload.get("answer", "")
        citation = payload.get("citation", "")
    except Exception as e:
        tool_call("done", {"answer": f"could not parse ask result: {e}",
                            "citation": "n/a"})
        return

    tool_call("done", {"answer": answer or "(empty answer)",
                        "citation": citation or "ReAct over the knowledge graph"})


main()
