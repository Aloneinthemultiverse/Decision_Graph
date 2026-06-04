"""Write OS/AgentNet build decisions INTO the shared DG *through the server*.

The DG server (port 8000) owns each workspace and caches it in memory, so writing
files directly desyncs the UI. Instead we POST decisions to the server's
`/api/mcp/call/track_decision` endpoint, bound to a fixed workspace via the
`dg_ws` cookie. The server applies them to its live workspace → the UI's
Knowledge Graph / Decision Memory views reflect them instantly.

This is the single shared channel DG ↔ {OS, AgentNet}.
"""
from __future__ import annotations
import json, urllib.request, urllib.error

DG_BASE = "http://localhost:8000"


def _post(base: str, token: str, tool: str, args: dict, timeout=20):
    body = json.dumps(args).encode()
    req = urllib.request.Request(
        f"{base}/api/mcp/call/{tool}", data=body,
        headers={"Content-Type": "application/json", "Cookie": f"dg_ws={token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def server_up(base: str = DG_BASE) -> bool:
    try:
        urllib.request.urlopen(f"{base}/api/status", timeout=4)
        return True
    except Exception:
        return False


def track_decision(token: str, question: str, answer: str, reasoning: str,
                   topic: str = "", base: str = DG_BASE) -> bool:
    """Log one decision into the shared DG workspace via the server. Returns ok."""
    try:
        _post(base, token, "track_decision", {
            "question": question[:300], "answer": answer[:600],
            "reasoning": reasoning[:600], "topic": topic[:80]})
        return True
    except Exception:
        return False


def sync_build(token: str, task: str, repo: str, subtasks: list[dict],
               summary: dict, base: str = DG_BASE) -> dict:
    """Push a whole build's decisions into the shared DG workspace `token`:
      - one decision per subtask (agent + skills + files), and
      - one build-summary decision.
    Best-effort; never raises. Returns {ok, posted}."""
    if not server_up(base):
        return {"ok": False, "posted": 0, "reason": "DG server (8000) not running"}
    posted = 0
    for s in subtasks:
        agent = s.get("builder") or (s.get("agents") or ["?"])[0]
        files = ", ".join(s.get("files") or []) or "(none)"
        if track_decision(
                token,
                question=f"[{repo}] {s.get('step','')}: {s.get('title','')}",
                answer=f"{agent} built {len(s.get('files') or [])} file(s): {files}",
                reasoning=f"skills={s.get('skills')} rules={s.get('rules')} "
                          f"status={s.get('status')}",
                topic=repo, base=base):
            posted += 1
    # build summary
    if track_decision(
            token,
            question=f"[{repo}] BUILD COMPLETE: {task[:120]}",
            answer=f"{summary.get('files',0)} files · {summary.get('symbols',0)} symbols "
                   f"· {summary.get('calls',0)} call edges · boot={summary.get('boot','?')}",
            reasoning=f"task_type={summary.get('task_type','')} "
                      f"agents={summary.get('agents',[])}",
            topic=repo, base=base):
        posted += 1
    return {"ok": True, "posted": posted}
