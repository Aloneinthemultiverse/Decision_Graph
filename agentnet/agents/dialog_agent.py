"""Dialog agent — long-lived, speaks MCP via stdio, participates in a
multi-agent debate.

Lifecycle inside the sandbox:
  1) Read /sandbox/task.json -> {goal, task, topics, run_id, agent_name, max_rounds, peers}
  2) Form initial position via `ask` (host-side ReAct grounded in the graph)
  3) Broadcast initial position via `send_message`
  4) Loop up to max_rounds:
       - `wait_for_message` (blocks up to 20s for peer messages)
       - if got messages: respond via `ask` with the peer context
       - else: break early (peers are quiet, dialog is over)
  5) Final `ask` to synthesize a closing position
  6) `done` with the closing position
"""
import json
import sys


try:
    JOB = json.load(open("/sandbox/task.json"))
except Exception:
    JOB = {}

GOAL = JOB.get("goal", "")
TASK = JOB.get("task", "")
TOPICS = JOB.get("topics", []) or []
RUN_ID = JOB.get("run_id", "")
AGENT_NAME = JOB.get("agent_name", "agent")
MAX_ROUNDS = max(1, int(JOB.get("max_rounds", 3)))
PEERS = JOB.get("peers", []) or []


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


def main():
    rpc("initialize")
    rpc("tools/list")

    primary_topic = TOPICS[0] if TOPICS else "general"
    peer_list_str = ", ".join(PEERS) if PEERS else "(unknown)"

    # 1) initial position
    initial_q = (f"Goal of debate: {GOAL}\n"
                 f"Your role: {AGENT_NAME}. Your task: {TASK}.\n"
                 f"Peers in the room: {peer_list_str}.\n\n"
                 f"Form a clear, specific, opinionated initial position on the goal "
                 f"from your role's perspective. 2-4 sentences. Be concrete.")
    r = tool("ask", {"topic": primary_topic, "question": initial_q})
    p = parse(r) or {}
    opinion = (p.get("answer") or "").strip() or "Initial position pending."
    tool("post_message", {"to": "all",
                           "text": f"[INITIAL · {AGENT_NAME}]\n{opinion[:1500]}"})

    last_position = opinion
    since_ts = 0.0

    # 2) loop
    for rnd in range(1, MAX_ROUNDS + 1):
        r = tool("wait_for_message",
                 {"timeout_seconds": 25, "since_ts": since_ts})
        p = parse(r) or {}
        msgs = p.get("messages") or []
        if not msgs:
            # peers silent — stop early
            break

        since_ts = max((m.get("ts", 0) for m in msgs), default=since_ts)

        # condense peer context
        peer_text = "\n\n".join(
            f"[{m.get('from','peer')}]: {(m.get('text','') or '')[:600]}"
            for m in msgs[-6:])

        response_q = (
            f"Goal of debate: {GOAL}\n"
            f"Your role: {AGENT_NAME}. Your task: {TASK}.\n"
            f"Round {rnd} of {MAX_ROUNDS}.\n\n"
            f"Recent messages from peers:\n{peer_text}\n\n"
            f"Your last position was:\n{last_position[:1000]}\n\n"
            f"Respond as {AGENT_NAME}. Address the peers' points SPECIFICALLY. "
            f"Concede where they have a point; defend where you disagree. "
            f"Refine your position if needed. Keep it 3-6 sentences and "
            f"concrete — no waffle.")

        r2 = tool("ask", {"topic": primary_topic, "question": response_q})
        p2 = parse(r2) or {}
        reply = (p2.get("answer") or "").strip()
        if not reply:
            reply = "I have nothing new to add this round."
        last_position = reply
        tool("post_message", {"to": "all",
                               "text": f"[R{rnd} · {AGENT_NAME}]\n{reply[:1500]}"})

    # 3) closing synthesis
    final_q = (
        f"Goal of debate: {GOAL}\n"
        f"Your role: {AGENT_NAME}.\n\n"
        f"After this multi-round debate, what is your final synthesized "
        f"position? 4-8 sentences. Include any concessions you made + your "
        f"firm conclusion. Speak as {AGENT_NAME}.")
    rf = tool("ask", {"topic": primary_topic, "question": final_q})
    pf = parse(rf) or {}
    final = (pf.get("answer") or last_position).strip() or last_position

    tool("done", {
        "answer": final,
        "citation": (f"dialog over {MAX_ROUNDS} rounds; peers: "
                      f"{peer_list_str}")})


main()
