"""Multi-agent orchestration — Phase 3 of AgentNet.

Pipeline mode: stage 1 -> stage 2 -> ... each stage mints its own scoped
grant, runs in its own sandbox, writes its learning back into the graph.
Stage N+1 reads the graph AFTER stage N has updated it, so each stage
benefits from the prior stages' work — without ever sharing a token.

Run history is persisted as JSON in the workspace's agent dir so the
dashboard / Run history table can show past orchestrations.

Coordinator mode is stubbed (404 for now) — that's the next thing to build.
"""
from __future__ import annotations

import os
import json
from typing import Optional
import time
import uuid

from .agent_access import AgentAccessStore
from .agent_catalog import get_agent, CatalogError
from .agent_job import (run_agent_job, run_agent_job_mcp,
                         run_agent_job_react, AgentJobError)


_RUNS_FILE = "orchestration_runs.json"


def _runs_path(store: AgentAccessStore) -> str:
    return os.path.join(store._dir, _RUNS_FILE)


def _load_runs(store: AgentAccessStore) -> list:
    try:
        with open(_runs_path(store), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_runs(store: AgentAccessStore, runs: list) -> None:
    p = _runs_path(store)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(runs[-200:], f)   # cap to last 200 runs
    os.replace(tmp, p)


def run_pipeline(store: AgentAccessStore, adapter, stages: list[dict]) -> dict:
    """Run a pipeline of agent stages, sequentially, on the same company graph.

    stages = [{ agent_id, topics:[str], task:str, pass_output_to_next?:bool }]

    Returns a run summary dict with per-stage results.
    """
    run_id = "orch_" + uuid.uuid4().hex[:10]
    started = time.time()
    stage_results = []
    prior_answer: str | None = None
    ok = True

    store.record_event("orchestration_started", run_id=run_id,
                       mode="pipeline", stages=len(stages))

    for idx, stage in enumerate(stages):
        agent_id = stage.get("agent_id", "")
        topics = stage.get("topics") or []
        task = stage.get("task") or "do the work"
        pass_through = bool(stage.get("pass_output_to_next", True))

        # if prior stage said pass-output, prepend its answer as context
        if prior_answer:
            task = (f"(prior-stage context: {prior_answer[:500]})\n"
                    f"{task}")

        try:
            a = get_agent(agent_id)
        except CatalogError as e:
            stage_results.append({"stage_index": idx, "ok": False,
                                  "error": str(e), "agent_id": agent_id})
            ok = False
            break

        grant = store.mint(agent_name=a["name"], allowed_topics=topics,
                           ttl_seconds=900, can_write=False)
        runner = (run_agent_job_mcp if "via-mcp" in (a.get("tags") or [])
                  else run_agent_job)
        try:
            job = runner(store=store, adapter=adapter,
                         agent_token=grant["token"], topics=topics,
                         agent_script=a["script_path"], task=task)
            stage_results.append({"stage_index": idx, "ok": True,
                                  "agent_id": agent_id, "job": job})
            if pass_through:
                prior_answer = (job.get("result") or {}).get("answer")
        except AgentJobError as e:
            stage_results.append({"stage_index": idx, "ok": False,
                                  "agent_id": agent_id, "error": str(e)})
            ok = False
            break

    finished = time.time()
    summary = {
        "run_id": run_id,
        "mode": "pipeline",
        "started_at": started,
        "finished_at": finished,
        "duration_s": round(finished - started, 3),
        "status": "ok" if ok else "failed",
        "stages_count": len(stages),
        "stages": stage_results,
        "final_answer_preview": prior_answer or "",
    }

    runs = _load_runs(store)
    # store a slim version in run history (no full job blobs)
    slim = {**summary, "stages": [{"stage_index": s["stage_index"],
                                    "agent_id": s.get("agent_id"),
                                    "ok": s["ok"],
                                    "learning_id": (s.get("job") or {}).get("learning_id"),
                                    "duration_s": (s.get("job") or {}).get("duration_s"),
                                    "error": s.get("error")}
                                   for s in stage_results]}
    runs.append(slim)
    _save_runs(store, runs)

    store.record_event("orchestration_completed" if ok else "orchestration_failed",
                       run_id=run_id, mode="pipeline",
                       stages=len(stages),
                       duration_s=summary["duration_s"])
    return summary


def run_swarm(store: AgentAccessStore, adapter, ws_dg,
              members: list[dict], goal: str = "",
              member_timeout_s: int = 600) -> dict:
    """Run N agents in PARALLEL — each in its own sandbox with its own
    scoped grant. All write to the same company DG.

    members = [{ agent_id, topics:[str], task:str }]
    """
    run_id = "swarm_" + uuid.uuid4().hex[:10]
    started = time.time()

    # mint a grant per member up front so audit shows them all spawned together
    plan = []
    for m in members:
        try:
            a = get_agent(m["agent_id"])
        except CatalogError as e:
            return {"run_id": run_id, "mode": "swarm",
                    "status": "failed",
                    "error": f"unknown agent_id '{m['agent_id']}': {e}"}
        grant = store.mint(
            agent_name=a["name"],
            allowed_topics=m.get("topics") or [],
            ttl_seconds=900, can_write=False)
        plan.append((a, grant, m))

    store.record_event("swarm_started", run_id=run_id, mode="swarm",
                       members=len(plan), goal=(goal or "")[:200])

    import threading
    results: list[dict] = [None] * len(plan)   # type: ignore

    def _worker(i: int, a: dict, grant: dict, m: dict):
        topics = m.get("topics") or []
        task = m.get("task") or (f"Contribute to the orchestration goal: {goal}"
                                  if goal else "Do your assigned analysis.")
        tags = a.get("tags") or []
        use_react = "via-react" in tags
        use_mcp = "via-mcp" in tags
        try:
            if use_react:
                results[i] = run_agent_job_react(
                    store=store, ws_dg=ws_dg, agent_token=grant["token"],
                    topics=topics, task=task)
            elif use_mcp:
                results[i] = run_agent_job_mcp(
                    store=store, adapter=adapter, agent_token=grant["token"],
                    topics=topics, agent_script=a["script_path"], task=task)
            else:
                results[i] = run_agent_job(
                    store=store, adapter=adapter, agent_token=grant["token"],
                    topics=topics, agent_script=a["script_path"], task=task)
            results[i]["agent_id"] = m["agent_id"]
            results[i]["stage_index"] = i
            results[i]["ok"] = True
        except AgentJobError as e:
            results[i] = {"ok": False, "stage_index": i,
                          "agent_id": m["agent_id"], "error": str(e)}
        except Exception as e:
            results[i] = {"ok": False, "stage_index": i,
                          "agent_id": m["agent_id"],
                          "error": f"{type(e).__name__}: {e}"}

    threads: list[threading.Thread] = []
    for i, (a, grant, m) in enumerate(plan):
        t = threading.Thread(target=_worker, args=(i, a, grant, m),
                             daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=member_timeout_s)

    finished = time.time()
    duration = round(finished - started, 3)
    ok = all((r or {}).get("ok") for r in results)
    summary = {
        "run_id": run_id,
        "mode": "swarm",
        "goal": goal,
        "started_at": started,
        "finished_at": finished,
        "duration_s": duration,
        "status": "ok" if ok else "partial",
        "members_count": len(plan),
        "members": results,
    }

    runs = _load_runs(store)
    slim = {**summary, "members": [
        {"stage_index": r["stage_index"],
         "agent_id": r.get("agent_id"),
         "ok": r.get("ok", False),
         "learning_id": (r.get("learning_id") if isinstance(r, dict) else None),
         "duration_s": (r.get("duration_s") if isinstance(r, dict) else None),
         "error": r.get("error")}
        for r in results]}
    runs.append(slim)
    _save_runs(store, runs)

    store.record_event(
        "swarm_completed" if ok else "swarm_partial",
        run_id=run_id, mode="swarm",
        members=len(plan), duration_s=duration)
    return summary


def run_dialog(store: AgentAccessStore, adapter, ws_dg,
               members: list[dict], goal: str = "",
               max_rounds: int = 3,
               member_timeout_s: int = 600) -> dict:
    """Multi-agent dialog. N agents run in PARALLEL sandboxes; each speaks
    MCP over stdio; they communicate through a shared per-run message bus.

    Each member: { agent_id, agent_role?, topics:[str], task:str }
    `agent_role` overrides the catalog name for this run (so you can have
    "skeptic" and "advocate" as roles even using the same underlying agent).
    """
    run_id = "dialog_" + uuid.uuid4().hex[:10]
    started = time.time()

    # mint per-member grants up front so audit shows the cohort spawning together
    plan = []
    for m in members:
        try:
            a = get_agent(m["agent_id"])
        except CatalogError as e:
            return {"run_id": run_id, "mode": "dialog",
                    "status": "failed",
                    "error": f"unknown agent_id '{m['agent_id']}': {e}"}
        role = (m.get("agent_role") or a["name"]).strip() or a["name"]
        grant = store.mint(
            agent_name=role,
            allowed_topics=m.get("topics") or [],
            ttl_seconds=1800, can_write=False)
        plan.append((a, grant, m, role))

    peer_roles = [role for (_, _, _, role) in plan]
    from .agent_messages import get_bus
    bus = get_bus(store.ws_root)

    store.record_event("dialog_started", run_id=run_id, mode="dialog",
                       members=len(plan), goal=(goal or "")[:200],
                       max_rounds=max_rounds, peers=peer_roles)

    import threading
    from .mcp_gateway import MCPGateway
    from .agent_sandbox import run_sandboxed_mcp, SandboxConfig

    results: list[dict] = [None] * len(plan)   # type: ignore

    def _worker(i: int, a: dict, grant: dict, m: dict, role: str):
        topics = m.get("topics") or []
        task = m.get("task") or ""
        gw = MCPGateway(
            store=store, adapter=adapter,
            agent_token=grant["token"],
            job_id=f"dlg_{run_id}_{i}",
            dg=ws_dg, ws=None,
            ctx={"JOBS": None, "S_simulator": None, "settings": {},
                 "bus": bus, "run_id": run_id, "agent_name": role,
                 "max_rounds": max_rounds})
        task_payload = {
            "goal": goal, "task": task, "topics": topics,
            "run_id": run_id, "agent_name": role,
            "max_rounds": max_rounds,
            "peers": [p for p in peer_roles if p != role],
        }
        try:
            run = run_sandboxed_mcp(
                agent_script=a["script_path"],
                gateway=gw, task_payload=task_payload,
                cfg=SandboxConfig(timeout_s=max(120,
                                                  max_rounds * 90 + 90)),
                max_calls=128)
            results[i] = {
                "ok": True, "stage_index": i,
                "agent_id": m["agent_id"], "agent_role": role,
                "final_answer": (gw.final_answer or {}).get("answer", ""),
                "citation": (gw.final_answer or {}).get("citation", ""),
                "mcp_calls": gw.call_count,
                "duration_s": run.get("duration_s"),
            }
        except Exception as e:
            results[i] = {"ok": False, "stage_index": i,
                          "agent_id": m["agent_id"], "agent_role": role,
                          "error": f"{type(e).__name__}: {e}"}

    threads: list[threading.Thread] = []
    for i, (a, grant, m, role) in enumerate(plan):
        t = threading.Thread(target=_worker,
                              args=(i, a, grant, m, role), daemon=True)
        t.start(); threads.append(t)
    for t in threads:
        t.join(timeout=member_timeout_s)

    finished = time.time()
    duration = round(finished - started, 3)
    ok = all((r or {}).get("ok") for r in results)
    transcript = bus.read_all_for_run(run_id)

    # Each dialog member's final synthesis is institutional knowledge — write
    # them back into the company DG memory so they can be retrieved later.
    # Each becomes a decision tagged with the run_id so it can be traced.
    learnings_written: list[str] = []
    try:
        for r in results:
            if not (r and r.get("ok")):
                continue
            ans = (r.get("final_answer") or "").strip()
            if not ans:
                continue
            role = r.get("agent_role") or "agent"
            try:
                did = ws_dg.memory.store(
                    question=f"[dialog:{role}] {goal}"[:500],
                    answer=ans[:4000],
                    reasoning_summary=(
                        f"Final position from dialog {run_id} "
                        f"({len(transcript)} messages, {max_rounds} rounds, "
                        f"peers={[r2.get('agent_role') for r2 in results if r2 and r2 is not r]})"
                    )[:1000],
                    communities_used=[], context_triples=[])
                r["learning_id"] = did
                learnings_written.append(did)
            except Exception:
                pass
        if learnings_written:
            ws_dg.memory.save()
    except Exception:
        pass

    summary = {
        "run_id": run_id,
        "mode": "dialog",
        "goal": goal,
        "max_rounds": max_rounds,
        "started_at": started,
        "finished_at": finished,
        "duration_s": duration,
        "status": "ok" if ok else "partial",
        "members_count": len(plan),
        "members": results,
        "transcript": transcript,
        "learnings_written": learnings_written,
    }

    runs = _load_runs(store)
    slim = {**summary,
            # don't bloat run history with the full transcript; keep counts
            "transcript_count": len(transcript),
            "transcript": [],
            "members": [
                {"stage_index": r.get("stage_index"),
                 "agent_id": r.get("agent_id"),
                 "agent_role": r.get("agent_role"),
                 "ok": r.get("ok", False),
                 "mcp_calls": r.get("mcp_calls"),
                 "duration_s": r.get("duration_s"),
                 "error": r.get("error")}
                for r in results]}
    runs.append(slim)
    _save_runs(store, runs)

    store.record_event(
        "dialog_completed" if ok else "dialog_partial",
        run_id=run_id, members=len(plan), duration_s=duration,
        transcript_count=len(transcript))
    return summary


def run_coordinator(store: AgentAccessStore, adapter, ws_dg,
                     goal: str,
                     allowed_agent_ids: Optional[list[str]] = None,
                     max_hires: int = 5,
                     max_seconds: int = 300,
                     allowed_topics: Optional[list[str]] = None) -> dict:
    """Autonomous coordinator mode (Phase 4 G).

    The host runs a ReAct loop using the workspace's LLM. At each step the
    LLM sees the goal + a list of agents in the catalog it's allowed to
    hire + previous sub-hire results. It decides:
        - hire <agent_id> for <topic(s)> with <task>
        - or: ANSWER: <final synthesis>

    Hard limits prevent runaway: max_hires, max_seconds, allowed_topics
    must already be in the manager's grant.
    """
    import time as _t
    started = _t.time()
    run_id = "coord_" + uuid.uuid4().hex[:10]
    store.record_event("coordinator_started", run_id=run_id, goal=goal[:300],
                       max_hires=max_hires, max_seconds=max_seconds)

    # Catalog filtered to allowed agents (if specified)
    try:
        from .agent_catalog import load_catalog
        all_agents = load_catalog()
    except Exception:
        all_agents = []
    if allowed_agent_ids:
        catalog = [a for a in all_agents if a["id"] in set(allowed_agent_ids)]
    else:
        catalog = [a for a in all_agents if a.get("available")]
    if not catalog:
        return {"run_id": run_id, "mode": "coordinator", "status": "failed",
                "error": "no available agents in the allowed catalog"}

    catalog_text = "\n".join(
        f"  - {a['id']}: {a['description'][:140]} "
        f"(tags={a.get('tags', [])})"
        for a in catalog)

    hires: list[dict] = []
    transcript: list[str] = []

    # The LLM-driven planner loop
    client = ws_dg.client
    from .config import LLM_MODEL

    sys_prompt = (
        f"You are an autonomous coordinator. Your job: solve the goal by "
        f"hiring sub-agents from the available catalog, observing their "
        f"outputs, and synthesizing a final answer.\n\n"
        f"GOAL:\n{goal}\n\n"
        f"AVAILABLE AGENTS (id : description):\n{catalog_text}\n\n"
        f"BUDGET: at most {max_hires} sub-hires; max {max_seconds} seconds.\n"
        f"You have made {{n_hires_so_far}} hires so far.\n\n"
        f"You may hire by emitting exactly:\n"
        f'    HIRE: {{ "agent_id": "...", "topics": ["..."], '
        f'"task": "..." }}\n'
        f"or finish by emitting:\n"
        f"    ANSWER: <your final synthesized answer in prose>\n\n"
        f"Output ONLY one of those — no extra commentary."
    )

    def _llm(prompt: str) -> str:
        try:
            r = client.messages.create(
                model=LLM_MODEL, max_tokens=2000,
                messages=[{"role": "user", "content": prompt}])
            return next((b.text for b in r.content if b.type == "text"
                          and b.text.strip()), "")
        except Exception as e:
            return f"ANSWER: coordinator LLM call failed: {type(e).__name__}: {e}"

    def _failed(reason: str):
        store.record_event("coordinator_failed", run_id=run_id, reason=reason)
        return {"run_id": run_id, "mode": "coordinator", "status": "failed",
                "error": reason, "hires": hires, "transcript": transcript,
                "duration_s": round(_t.time() - started, 3)}

    while True:
        if len(hires) >= max_hires:
            transcript.append("Budget reached: max_hires hit.")
            break
        if _t.time() - started > max_seconds:
            transcript.append("Budget reached: max_seconds hit.")
            break

        # Build context from past hires
        ctx = ""
        for i, h in enumerate(hires, 1):
            j = h.get("job") or {}
            res = (j.get("result") or {})
            ans = res.get("answer", "")[:600]
            ctx += (f"\n[Hire {i}: agent={h['agent_id']}, "
                    f"topics={h['topics']}, task={h['task'][:120]}]\n"
                    f"-> {ans}\n")

        prompt = sys_prompt.replace("{n_hires_so_far}", str(len(hires)))
        if ctx:
            prompt += "\nPAST HIRES + RESULTS:" + ctx
        prompt += "\n\nWhat do you do next? Reply with HIRE: or ANSWER: only."

        decision = _llm(prompt).strip()
        transcript.append(f"PLAN[{len(hires)}]: {decision[:300]}")

        if decision.startswith("ANSWER:"):
            final = decision[len("ANSWER:"):].strip()
            duration = round(_t.time() - started, 3)
            # persist the final synthesis as a decision in DG memory
            try:
                ws_dg.memory.store(
                    question=f"[coordinator] {goal}"[:500],
                    answer=final[:4000],
                    reasoning_summary=(
                        f"Autonomous coordinator run {run_id}: "
                        f"{len(hires)} sub-hires; goal answered by manager."),
                    communities_used=[], context_triples=[])
                ws_dg.memory.save()
            except Exception:
                pass
            store.record_event("coordinator_completed", run_id=run_id,
                               hires=len(hires), duration_s=duration)
            return {
                "run_id": run_id, "mode": "coordinator", "status": "ok",
                "goal": goal, "final_answer": final,
                "hires": hires, "transcript": transcript,
                "duration_s": duration,
            }

        if not decision.startswith("HIRE:"):
            # nudge the LLM once; otherwise abort
            transcript.append("Manager produced no HIRE/ANSWER; aborting.")
            return _failed("manager LLM produced no valid command")

        # Parse the HIRE JSON
        try:
            body = decision[len("HIRE:"):].strip()
            # tolerate trailing junk after the JSON object
            if body.startswith("{"):
                depth = 0; end = -1
                for i, ch in enumerate(body):
                    if ch == "{": depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1; break
                body = body[:end] if end > 0 else body
            plan = json.loads(body)
        except Exception as e:
            transcript.append(f"Bad HIRE JSON: {e}")
            return _failed(f"manager produced invalid HIRE JSON: {e}")

        sub_agent_id = plan.get("agent_id")
        sub_topics = plan.get("topics") or []
        sub_task = (plan.get("task") or "").strip()
        if sub_agent_id not in {a["id"] for a in catalog}:
            transcript.append(f"Disallowed agent_id={sub_agent_id}; aborting.")
            return _failed(f"manager tried to hire disallowed agent {sub_agent_id}")
        if allowed_topics is not None:
            sub_topics = [t for t in sub_topics if t in allowed_topics]
            if not sub_topics:
                transcript.append("All requested topics outside coordinator scope.")
                continue

        # Execute the sub-hire through the existing lifecycle
        try:
            sub_agent = next(a for a in catalog if a["id"] == sub_agent_id)
            grant = store.mint(agent_name=sub_agent["name"],
                                allowed_topics=sub_topics,
                                ttl_seconds=600, can_write=False)
            tags = sub_agent.get("tags") or []
            if "via-react" in tags:
                sub_summary = run_agent_job_react(
                    store=store, ws_dg=ws_dg, agent_token=grant["token"],
                    topics=sub_topics, task=sub_task)
            elif "via-mcp" in tags:
                sub_summary = run_agent_job_mcp(
                    store=store, adapter=adapter, agent_token=grant["token"],
                    topics=sub_topics, agent_script=sub_agent["script_path"],
                    task=sub_task)
            else:
                sub_summary = run_agent_job(
                    store=store, adapter=adapter, agent_token=grant["token"],
                    topics=sub_topics, agent_script=sub_agent["script_path"],
                    task=sub_task)
            hires.append({"agent_id": sub_agent_id, "topics": sub_topics,
                          "task": sub_task, "job": sub_summary})
            transcript.append(f"  → hired {sub_agent_id} OK · learning="
                              f"{sub_summary.get('learning_id')}")
        except AgentJobError as e:
            hires.append({"agent_id": sub_agent_id, "topics": sub_topics,
                          "task": sub_task,
                          "error": f"AgentJobError: {e}"})
            transcript.append(f"  → hire FAILED: {e}")
            # continue — let the manager react to the failure

    # Budget exhausted without ANSWER; force one final synthesis
    final = _llm(
        sys_prompt.replace("{n_hires_so_far}", str(len(hires))) +
        "\nBUDGET EXHAUSTED. Synthesize the final ANSWER now using whatever "
        "you've learned.\nReply with: ANSWER: <prose>"
    )
    if final.startswith("ANSWER:"):
        final = final[len("ANSWER:"):].strip()
    duration = round(_t.time() - started, 3)
    store.record_event("coordinator_completed", run_id=run_id,
                       hires=len(hires), duration_s=duration,
                       budget_exhausted=True)
    return {
        "run_id": run_id, "mode": "coordinator",
        "status": "budget_exhausted",
        "goal": goal, "final_answer": final,
        "hires": hires, "transcript": transcript,
        "duration_s": duration,
    }


def list_runs(store: AgentAccessStore, limit: int = 50) -> list:
    return list(reversed(_load_runs(store)))[:limit]


def get_run(store: AgentAccessStore, run_id: str) -> dict | None:
    for r in _load_runs(store):
        if r.get("run_id") == run_id:
            return r
    return None
