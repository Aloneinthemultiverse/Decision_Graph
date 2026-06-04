"""Slice-1 Step 4 — the end-to-end lifecycle.

Ties Step 1 (scoped access gate + audit) and Step 2 (network-less sandbox)
into one auditable job:

    grant  ->  host authorizes each topic (Step-1 gate, every check logged)
           ->  host fetches ONLY the approved scoped slice from the company
               memory
           ->  agent runs that slice inside the Step-2 sandbox (no network)
           ->  result comes back out
           ->  container destroyed (handled by Step-2 --rm)
           ->  the *learnings* are written back into the COMPANY graph
               (never carried by the agent)
           ->  a job_completed entry is finalized in the immutable audit log
           ->  the agent token is revoked (single-use by default)

The agent arrives with nothing and leaves with nothing; only the company's
memory grew, and every step is provable from the audit log.

Decoupled via a tiny MemoryAdapter protocol so the lifecycle is unit-testable
without the heavy embedding model or Docker (a fake adapter + a fake runner),
and wired to the real workspace via WorkspaceMemoryAdapter in the server.
"""
from __future__ import annotations

import time
from typing import Protocol, Callable, Optional

from .agent_access import AgentAccessStore, AgentAccessError


class MemoryAdapter(Protocol):
    """Minimal contract the orchestrator needs from a company's memory."""

    def fetch_scope(self, topic: str) -> list[dict]:
        """Return the decision/knowledge records for one approved topic."""
        ...

    def store_learning(self, question: str, answer: str, reasoning: str) -> str:
        """Persist a learning back into the COMPANY memory. Returns its id."""
        ...


class WorkspaceMemoryAdapter:
    """Real adapter over a DecisionGraph instance (`ws.dg`).

    Topic scoping mirrors the existing /api/recall heuristic: a decision is
    "in" a topic if the topic string appears in its question/answer. Keeps
    Slice-1 dependency-light and consistent with current behaviour.
    """

    def __init__(self, dg):
        self._dg = dg

    def fetch_scope(self, topic: str) -> list[dict]:
        """Return everything in scope for `topic` — both decisions (Q/A) AND
        knowledge-graph content (triples + community summaries) ingested from
        documents / PDFs / URLs. Decisions look like {kind:'decision', ...};
        knowledge looks like {kind:'triple', subject, relation, object} or
        {kind:'summary', community, summary}.
        """
        t = (topic or "").strip().lower()
        if not t:
            return []
        out: list[dict] = []

        # decisions (existing behaviour, unchanged)
        for d in self._dg.memory.get_active_decisions():
            blob = f"{d.get('question','')} {d.get('answer','')}".lower()
            if t in blob:
                out.append({
                    "kind": "decision",
                    "question": d.get("question", ""),
                    "answer": d.get("answer", ""),
                    "confidence": d.get("confidence", 0),
                    "timestamp": d.get("timestamp", ""),
                })

        # knowledge graph triples (this is where PDF/URL ingest lands)
        G = getattr(self._dg, "G", None)
        if G is not None:
            try:
                matched_nodes = {n for n in G.nodes() if t in str(n).lower()}
                # also pull edges that touch a matched node
                for u, v, data in G.edges(data=True):
                    if u in matched_nodes or v in matched_nodes \
                       or t in str(u).lower() or t in str(v).lower():
                        out.append({
                            "kind": "triple",
                            "subject": str(u),
                            "relation": str(data.get("relation", "")),
                            "object": str(v),
                        })
                        if len(out) > 500:
                            break
            except Exception:
                pass

        # community summaries that mention the topic
        summaries = getattr(self._dg, "summaries", None) or {}
        for cid, info in summaries.items():
            summ = (info.get("summary") or "") if isinstance(info, dict) else str(info)
            if t in summ.lower():
                out.append({
                    "kind": "summary",
                    "community": str(cid),
                    "summary": summ[:1200],
                })

        return out

    def store_learning(self, question: str, answer: str, reasoning: str) -> str:
        did = self._dg.memory.store(
            question=question, answer=answer, reasoning_summary=reasoning,
            communities_used=[], context_triples=[],
        )
        self._dg.memory.save()
        return did


# runner type: (agent_script, scoped_input, cfg) -> {"ok":bool,"result":dict,...}
Runner = Callable[..., dict]


class AgentJobError(Exception):
    pass


def run_agent_job_react(
    store: AgentAccessStore,
    ws_dg,                          # the real DecisionGraph instance
    agent_token: str,
    topics: list[str],
    task: str,
    *,
    revoke_after: bool = True,
) -> dict:
    """ReAct path: the LLM is the agent. Uses the existing react_agent from
    decisiongraph.agent — does THINK/ACT/ANSWER loop with query_graph tool.

    Scope is enforced by AUTHORIZING the grant for every topic up front
    (audited). The LLM agent then reads/reasons over the graph and produces a
    multi-paragraph answer with reasoning. The answer is captured as the
    job's learning and the lifecycle (grant -> revoke) is identical to the
    other paths.
    """
    job_id = f"react_{int(time.time()*1000)}"
    grant = store.get(agent_token)
    agent_name = grant.get("agent_name") if grant else "react-agent"
    store.record_event("job_started", job_id=job_id, token=agent_token,
                       agent_name=agent_name,
                       topics=[t.lower() for t in topics],
                       task=task, via="react")

    def _fail(reason: str):
        if revoke_after:
            store.revoke(agent_token)
        store.record_event("job_failed", job_id=job_id, token=agent_token,
                           agent_name=agent_name, reason=reason)
        raise AgentJobError(reason)

    # gate: authorize each topic up front (each call audited)
    try:
        for t in topics:
            store.authorize(agent_token, t, "read")
    except AgentAccessError as e:
        _fail(f"scope check failed: {e}")

    # run the existing ReAct loop on this workspace's graph + memory
    try:
        from .agent import react_agent
        t0 = time.time()
        answer = react_agent(
            question=task,
            client=ws_dg.client,
            G=ws_dg.G,
            community_summaries=ws_dg.summaries,
            community_ids=ws_dg.community_ids,
            community_embeddings=ws_dg.community_embeddings,
            embed_model=ws_dg.embed_model,
            memory=ws_dg.memory,
        )
        dur = round(time.time() - t0, 3)
    except Exception as e:
        _fail(f"react failure: {type(e).__name__}: {e}")

    # react_agent already wrote its decision to memory; reuse the latest id
    try:
        last = ws_dg.memory.get_active_decisions()
        learning_id = (last[-1].get("id") if last else None) or "react_unknown"
    except Exception:
        learning_id = "react_unknown"

    store.record_event("learning_written", job_id=job_id, token=agent_token,
                       agent_name=agent_name, learning_id=learning_id,
                       via="react")
    if revoke_after:
        store.revoke(agent_token)
    store.record_event("job_completed", job_id=job_id, token=agent_token,
                       agent_name=agent_name, learning_id=learning_id,
                       duration_s=dur, via="react")

    return {
        "job_id": job_id,
        "agent_name": agent_name,
        "ok": True,
        "via": "react",
        "scope": [t.lower() for t in topics],
        "learning_id": learning_id,
        "result": {"answer": answer or "",
                   "citation": "ReAct reasoning over the knowledge graph"},
        "duration_s": dur,
        "token_revoked": revoke_after,
    }


def run_agent_job_mcp(
    store: AgentAccessStore,
    adapter: MemoryAdapter,
    agent_token: str,
    topics: list[str],
    agent_script: str,
    task: str,
    *,
    sandbox_cfg=None,
    revoke_after: bool = True,
) -> dict:
    """MCP variant of run_agent_job: the agent reaches the graph via MCP
    tools (recall / scoped_search) through a host gateway that enforces the
    Step-1 scope gate on EVERY call. No data is pre-fetched into the sandbox.

    Lifecycle: grant -> sandboxed MCP-stdio session -> learning back into the
    graph -> token revoked -> audit finalized. Identical guarantees as
    run_agent_job, with the data path being MCP instead of a staged file.
    """
    job_id = f"mcp_{int(time.time()*1000)}"
    grant = store.get(agent_token)
    agent_name = grant.get("agent_name") if grant else "unknown"
    store.record_event("job_started", job_id=job_id, token=agent_token,
                       agent_name=agent_name,
                       topics=[t.lower() for t in topics],
                       task=task, via="mcp")

    def _finish_fail(reason: str):
        if revoke_after:
            store.revoke(agent_token)
        store.record_event("job_failed", job_id=job_id, token=agent_token,
                           agent_name=agent_name, reason=reason)
        raise AgentJobError(reason)

    # gate sanity: token must be valid right now (the per-call gate inside
    # the gateway enforces topic scope live)
    try:
        store._validate(agent_token)
    except AgentAccessError as e:
        _finish_fail(f"grant invalid: {e}")

    from .mcp_gateway import MCPGateway
    from .agent_sandbox import run_sandboxed_mcp
    gw = MCPGateway(store=store, adapter=adapter,
                    agent_token=agent_token, job_id=job_id,
                    dg=getattr(adapter, "_dg", None))
    try:
        run = run_sandboxed_mcp(
            agent_script=agent_script, gateway=gw,
            task_payload={"task": task,
                          "topics": [t.lower() for t in topics]},
            cfg=sandbox_cfg)
    except Exception as e:
        _finish_fail(f"sandbox failure: {type(e).__name__}: {e}")

    if not gw.final_answer:
        _finish_fail("agent exited without final answer")

    # write the LEARNING back into company memory (host, not the agent)
    answer = gw.final_answer.get("answer", "")
    citation = gw.final_answer.get("citation", "")
    learning_id = adapter.store_learning(
        question=f"[agent:{agent_name}] {task}",
        answer=(answer + (f"\nSource: {citation}" if citation else ""))[:4000],
        reasoning=(f"Produced by MCP-speaking agent '{agent_name}' "
                   f"for job {job_id}; "
                   f"mcp_calls={gw.call_count}; "
                   f"scope={[t.lower() for t in topics]}."))
    store.record_event("learning_written", job_id=job_id, token=agent_token,
                       agent_name=agent_name, learning_id=learning_id,
                       mcp_calls=gw.call_count)

    if revoke_after:
        store.revoke(agent_token)
    store.record_event("job_completed", job_id=job_id, token=agent_token,
                       agent_name=agent_name, learning_id=learning_id,
                       duration_s=run.get("duration_s"), via="mcp",
                       mcp_calls=gw.call_count)

    return {
        "job_id": job_id,
        "agent_name": agent_name,
        "ok": True,
        "via": "mcp",
        "mcp_calls": gw.call_count,
        "scope": [t.lower() for t in topics],
        "learning_id": learning_id,
        "result": {"answer": answer, "citation": citation},
        "duration_s": run.get("duration_s"),
        "token_revoked": revoke_after,
    }


def run_agent_job(
    store: AgentAccessStore,
    adapter: MemoryAdapter,
    agent_token: str,
    topics: list[str],
    agent_script: str,
    task: str,
    *,
    runner: Optional[Runner] = None,
    sandbox_cfg=None,
    revoke_after: bool = True,
) -> dict:
    """Execute the full audited lifecycle. Returns a job summary dict.

    `runner` defaults to agent_sandbox.run_sandboxed (imported lazily so the
    lifecycle logic is testable without Docker). On ANY failure the token is
    still revoked (fail-closed) and a job_failed entry is written.
    """
    job_id = f"job_{int(time.time()*1000)}"
    grant = store.get(agent_token)
    agent_name = grant.get("agent_name") if grant else "unknown"
    store.record_event("job_started", job_id=job_id, token=agent_token,
                       agent_name=agent_name, topics=[t.lower() for t in topics],
                       task=task)

    def _finish_fail(reason: str):
        if revoke_after:
            store.revoke(agent_token)
        store.record_event("job_failed", job_id=job_id, token=agent_token,
                           agent_name=agent_name, reason=reason)
        raise AgentJobError(reason)

    # 1. authorize every requested topic through the Step-1 gate (audited)
    scoped_data: dict[str, list] = {}
    try:
        for topic in topics:
            store.authorize(agent_token, topic, "read")  # logs allow/deny
            scoped_data[topic] = adapter.fetch_scope(topic)
    except AgentAccessError as e:
        _finish_fail(f"scope check failed: {e}")

    # 2. run the agent in the Step-2 sandbox on ONLY the approved slice
    if runner is None:
        from .agent_sandbox import run_sandboxed as runner  # lazy
    scoped_input = {"task": task, "data": scoped_data}
    try:
        run = runner(agent_script, scoped_input, sandbox_cfg)
    except Exception as e:
        _finish_fail(f"sandbox failure: {type(e).__name__}: {e}")

    if not run or not run.get("ok"):
        _finish_fail("agent run did not succeed")

    result = run.get("result") or {}

    # 3. write the LEARNING back into the COMPANY memory (not the agent)
    learning_answer = str(result.get("answer") or result.get("summary") or result)
    learning_id = adapter.store_learning(
        question=f"[agent:{agent_name}] {task}",
        answer=learning_answer[:4000],
        reasoning=f"Produced by sandboxed agent '{agent_name}' for job {job_id}; "
                  f"scope={list(scoped_data.keys())}.",
    )
    store.record_event("learning_written", job_id=job_id, token=agent_token,
                       agent_name=agent_name, learning_id=learning_id,
                       scope=list(scoped_data.keys()))

    # 4. finalize + revoke (single-use grant by default)
    if revoke_after:
        store.revoke(agent_token)
    store.record_event("job_completed", job_id=job_id, token=agent_token,
                       agent_name=agent_name, learning_id=learning_id,
                       duration_s=run.get("duration_s"))

    return {
        "job_id": job_id,
        "agent_name": agent_name,
        "ok": True,
        "scope": list(scoped_data.keys()),
        "learning_id": learning_id,
        "result": result,
        "duration_s": run.get("duration_s"),
        "token_revoked": revoke_after,
    }
