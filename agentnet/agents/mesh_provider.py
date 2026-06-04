"""Mesh-provider agent — exposes a fixed set of utility tools to peers in
the same orchestration run.

Tools provided:
  calculate(expression)     -> {value} for restricted arithmetic
  word_count(text)          -> {words, chars, sentences}
  base64_encode(text)       -> {b64}

Pattern:
  1) Register each tool via register_peer_tool
  2) Loop: wait_for_message, handle each tool_request, post a tool_response
  3) Stop when no requests for `idle_timeout_s` consecutive seconds OR
     after `max_lifetime_s`
"""
import json
import sys
import time
import re
import base64


try:
    JOB = json.load(open("/sandbox/task.json"))
except Exception:
    JOB = {}

AGENT_NAME = JOB.get("agent_name", "Mesh Provider")
MAX_LIFETIME = max(60, int(JOB.get("max_lifetime_s", 600)))
IDLE_TIMEOUT = max(20, int(JOB.get("idle_timeout_s", 60)))


def rpc(method, params=None, _id=[0]):
    _id[0] += 1
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": _id[0],
                                  "method": method,
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


def tool(name, args):
    return rpc("tools/call", {"name": name, "arguments": args})


def parse(r):
    try:
        return json.loads(r["result"]["content"][0]["text"])
    except Exception:
        return None


# ── tool implementations ────────────────────────────────────────────────
def t_calculate(args):
    expr = (args.get("expression") or "").strip()
    if not re.fullmatch(r"[0-9+\-*/(). ]+", expr or ""):
        return {"error": "only basic arithmetic allowed: digits + - * / ( ) ."}
    try:
        # eval with empty builtins — only arithmetic possible
        return {"expression": expr, "value": eval(expr, {"__builtins__": {}}, {})}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def t_word_count(args):
    text = args.get("text") or ""
    words = len([w for w in re.split(r"\s+", text) if w])
    sentences = len([s for s in re.split(r"[.!?]+", text) if s.strip()])
    return {"words": words, "chars": len(text), "sentences": sentences}


def t_base64_encode(args):
    text = args.get("text") or ""
    return {"b64": base64.b64encode(text.encode("utf-8")).decode("ascii")}


PROVIDERS = {
    "calculate":      (t_calculate,
                       "Evaluate a restricted arithmetic expression. "
                       "Supports + - * / ( ) and decimals.",
                       {"type": "object",
                        "properties": {"expression": {"type": "string"}},
                        "required": ["expression"]}),
    "word_count":     (t_word_count,
                       "Count words, chars, and sentences in a text.",
                       {"type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"]}),
    "base64_encode":  (t_base64_encode,
                       "UTF-8 encode + base64-encode a string.",
                       {"type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"]}),
}


def main():
    rpc("initialize")
    rpc("tools/list")

    # declare all the tools we provide
    for name, (_fn, desc, schema) in PROVIDERS.items():
        tool("register_peer_tool",
             {"name": name, "description": desc, "input_schema": schema})

    deadline = time.time() + MAX_LIFETIME
    last_activity = time.time()
    since_ts = 0.0

    while time.time() < deadline:
        if time.time() - last_activity > IDLE_TIMEOUT:
            break
        r = tool("wait_for_message",
                 {"timeout_seconds": 25, "since_ts": since_ts})
        p = parse(r) or {}
        msgs = p.get("messages") or []
        if not msgs:
            continue
        for m in msgs:
            since_ts = max(since_ts, m.get("ts", 0))
            if m.get("kind") != "tool_request":
                continue
            try:
                body = json.loads(m.get("text", "{}"))
            except Exception:
                continue
            req_id = body.get("req_id")
            tool_name = body.get("tool_name")
            args_in = body.get("args") or {}
            entry = PROVIDERS.get(tool_name)
            if entry is None:
                result = {"error": f"unknown tool: {tool_name}",
                          "available": list(PROVIDERS.keys())}
            else:
                fn, _, _ = entry
                try:
                    result = fn(args_in)
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}

            tool("post_message", {
                "to": m.get("from"),
                "text": json.dumps({"req_id": req_id, "tool_name": tool_name,
                                     "result": result}),
                "kind": "tool_response"})
            last_activity = time.time()

    tool("done", {
        "answer": f"Mesh provider session ended for {AGENT_NAME}. "
                  f"Tools provided: {', '.join(PROVIDERS.keys())}.",
        "citation": "mesh-provider"})


main()
