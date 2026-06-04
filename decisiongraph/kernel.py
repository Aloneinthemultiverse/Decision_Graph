"""Phase 3 — the Kernel: the coordinator that runs a task through 4 jobs.

    TASK → 1. DISPATCH → 2. LOAD → 3. GRANT → 4. RUN+LOG

  1. DISPATCH  pick the right ECC agent for the task (keyword score + DG-risk
               tie-break: if the task touches god-nodes, prefer a careful agent).
  2. LOAD      get_context_pack(task_files) → preload the blueprint slice into a
               fresh KERNEL-CAG session (the hot working memory).
  3. GRANT     mint a scoped, expiring AgentNet grant (audited) for the agent.
  4. RUN+LOG   run the agent (via an injected executor — real = Claude Code,
               test = a fake), log every step to the audit log + KERNEL-CAG,
               write results back to DG, then digest the session.

The kernel does NOT contain an LLM. The actual agent execution is delegated to
an `executor` callable so the orchestration is deterministic and testable; the
loop (load → work → write-back → digest) is the part that makes the OS compound.
"""
from __future__ import annotations

import os
import re
import time
from typing import Callable, Optional

from .kernel_cag import KernelCAG
from .context_pack import get_context_pack


# ── agent catalog (ECC) ───────────────────────────────────────────────────────
_FM = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_STOP = set("the a an and or to of in on for with when use proactively this that "
            "is are be by as at from your you it its which who whom into only "
            "specialist specializing using used uses".split())
# tasks/agents that signal a high-risk change should prefer a careful reviewer
_CAREFUL_HINTS = ("review", "architect", "security", "audit", "careful", "refactor")


def _parse_frontmatter(text: str) -> dict:
    m = _FM.search(text)
    if not m:
        return {}
    fm: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
        else:
            v = v.strip('"').strip("'")
        fm[k] = v
    return fm


def load_agents(agents_dir: str) -> list[dict]:
    """Read ECC agents/*.md → [{name, description, tools, model, _words}]."""
    out = []
    if not os.path.isdir(agents_dir):
        return out
    for fn in sorted(os.listdir(agents_dir)):
        if not fn.endswith(".md"):
            continue
        try:
            with open(os.path.join(agents_dir, fn), "r", encoding="utf-8") as f:
                raw = f.read()
                fm = _parse_frontmatter(raw)
        except Exception:
            continue
        name = fm.get("name") or fn[:-3]
        desc = fm.get("description") or ""
        if not desc:
            continue
        # keep the persona BODY (text after frontmatter) — the builder writes code
        # AS this persona, so it needs the persona's full instructions, not a label.
        body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", raw, count=1, flags=re.DOTALL).strip()
        out.append({
            "name": name,
            "description": desc,
            "body": body,
            "tools": fm.get("tools") if isinstance(fm.get("tools"), list) else [],
            "model": fm.get("model") or "",
            "_words": _significant_words(desc),
        })
    return out


def _significant_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+-]{2,}", s.lower())
            if w not in _STOP}


class Kernel:
    def __init__(self, dg, ws_root: str, agents_dir: str,
                 storage_dir: Optional[str] = None, embed_model=None):
        self.dg = dg
        self.ws_root = ws_root
        self.agents_dir = agents_dir
        self.storage_dir = storage_dir or getattr(dg, "storage_dir", ws_root)
        self.agents = load_agents(agents_dir)
        self.embed_model = embed_model        # optional: semantic dispatch
        self._agent_vecs = None               # cached agent-description embeddings
        from .agent_access import AgentAccessStore
        self.access = AgentAccessStore(ws_root)

    def _ensure_agent_vecs(self):
        """Embed each agent's name+description once (cached)."""
        if self._agent_vecs is not None or self.embed_model is None:
            return
        texts = [f"{a['name']}. {a['description']}" for a in self.agents]
        self._agent_vecs = self.embed_model.encode(texts)

    # ── JOB 1: DISPATCH ───────────────────────────────────────────────────
    def dispatch(self, task: str, task_files: Optional[list[str]] = None,
                 high_risk: Optional[bool] = None) -> dict:
        """Pick the best agent. Keyword overlap with each agent's description,
        plus a DG-risk tie-break toward 'careful' agents when the change is
        high-risk (god-node touch). Returns {agent, score, candidates, reason}."""
        if not self.agents:
            return {"agent": None, "score": 0, "candidates": [],
                    "reason": "no agents available"}

        if high_risk is None:
            high_risk = self._is_high_risk(task_files or [])

        # semantic scoring when an embed model is available, else keyword overlap
        sims = None
        if self.embed_model is not None:
            try:
                import numpy as np
                self._ensure_agent_vecs()
                tv = self.embed_model.encode([task])[0]
                av = self._agent_vecs
                sims = (av @ tv) / ((np.linalg.norm(av, axis=1) * np.linalg.norm(tv)) + 1e-9)
            except Exception:
                sims = None

        tw = _significant_words(task)
        ranked = []
        for i, a in enumerate(self.agents):
            if sims is not None:
                score = float(sims[i])            # cosine similarity 0..1
                boost = 0.15
            else:
                score = float(len(tw & a["_words"]))   # keyword overlap
                boost = 1.5
            careful = any(h in a["name"].lower() or h in a["description"].lower()
                          for h in _CAREFUL_HINTS)
            if high_risk and careful:
                score += boost
            ranked.append((score, careful, a))

        ranked.sort(key=lambda r: r[0], reverse=True)
        best_score, best_careful, best = ranked[0]
        mode = "semantic cosine" if sims is not None else "keyword overlap"
        base = best_score - ((0.15 if sims is not None else 1.5) if (high_risk and best_careful) else 0)
        reason = (f"top {mode}={base:.2f}" if sims is not None
                  else f"top {mode}={int(base)}")
        if high_risk and best_careful:
            reason += " + careful-agent risk boost"
        return {
            "agent": best["name"],
            "score": best_score,
            "high_risk": high_risk,
            "candidates": [{"agent": a["name"], "score": s} for s, _, a in ranked[:5]],
            "reason": reason,
            "_agent_obj": best,
        }

    def _is_high_risk(self, task_files: list[str]) -> bool:
        """Consult DG: does any task file fall in the structural blast radius of
        a god-node? Best-effort — if the code graph is unavailable, returns False."""
        if not task_files:
            return False
        try:
            from . import code_graph as cg
            db = os.path.join(self.storage_dir, "code_graph.db")
            if not os.path.exists(db):
                return False
            topo = cg.analyze_topology(db, top_god=10)
            god_files = set()
            for g in (topo.get("god_nodes") or []):
                p = g.get("path") if isinstance(g, dict) else None
                if p:
                    god_files.add(p)
            return any(tf in god_files for tf in task_files)
        except Exception:
            return False

    # ── JOB 2: LOAD ───────────────────────────────────────────────────────
    def load(self, task_files: Optional[list[str]], repo: str = "",
             token_budget: int = 6000,
             session_id: Optional[str] = None) -> tuple[KernelCAG, dict]:
        """Build the context pack and preload it into a fresh KERNEL-CAG."""
        cag = KernelCAG(self.storage_dir, session_id=session_id)
        bp = self._find_blueprint(repo)
        pack = {"markdown": "", "included_files": [], "approx_tokens": 0}
        if bp:
            pack = get_context_pack(bp, task_files or [], token_budget=token_budget)
        cag.load_context_slice(pack)
        return cag, pack

    def _find_blueprint(self, repo: str = "") -> str:
        bp_dir = os.path.join(self.storage_dir, "blueprints")
        if not os.path.isdir(bp_dir):
            return ""
        if repo:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", repo.replace("/", "_"))[:120]
            cand = os.path.join(bp_dir, f"{safe}.md")
            if os.path.exists(cand):
                return cand
        mds = [f for f in os.listdir(bp_dir) if f.endswith(".md")]
        return os.path.join(bp_dir, mds[0]) if len(mds) == 1 else ""

    # ── JOB 3: GRANT ──────────────────────────────────────────────────────
    def grant(self, agent: dict, repo: str = "", task_files: Optional[list[str]] = None,
              ttl_seconds: int = 1800) -> dict:
        """Mint a scoped, expiring AgentNet grant. Topics = repo + touched
        folders. can_write iff the agent's ECC toolset includes Edit/Write/Bash."""
        topics = []
        if repo:
            topics.append(repo)
        for f in (task_files or []):
            folder = os.path.dirname(f.replace("\\", "/"))
            if folder:
                topics.append(folder)
        if not topics:
            topics = ["code"]
        write_tools = {"Edit", "Write", "MultiEdit", "Bash"}
        can_write = bool(set(agent.get("tools") or []) & write_tools)
        return self.access.mint(agent_name=agent["name"], allowed_topics=topics,
                                ttl_seconds=ttl_seconds, can_write=can_write)

    # ── JOB 4: RUN + LOG ──────────────────────────────────────────────────
    def run_task(self, task: str, task_files: Optional[list[str]] = None,
                 repo: str = "", token_budget: int = 6000,
                 ttl_seconds: int = 1800,
                 executor: Optional[Callable[[dict], dict]] = None,
                 force_agent: Optional[str] = None) -> dict:
        """Run one task through all 4 jobs. `executor(contract) -> result` is the
        agent runner (real = Claude Code; test/dry-run = a stub). The kernel
        audits the lifecycle, records the executor's tool calls / edits /
        decisions into KERNEL-CAG, writes results back to DG, and digests.

        force_agent: when the DISPATCHER (LLM) already chose the persona, pass its
        name here so it overrides the kernel's own heuristic pick."""
        t0 = time.time()

        # JOB 1
        disp = self.dispatch(task, task_files)
        if force_agent:
            forced = next((a for a in self.agents if a["name"] == force_agent), None)
            if forced:
                agent_obj = forced
                disp = {**disp, "agent": force_agent, "reason": "forced by dispatcher",
                        "_agent_obj": forced}
            else:
                agent_obj = disp.get("_agent_obj")
        else:
            agent_obj = disp.get("_agent_obj")
        if not agent_obj:
            return {"error": "dispatch failed: no agent", "dispatch": disp}

        # JOB 2
        cag, pack = self.load(task_files, repo=repo, token_budget=token_budget)

        # JOB 3
        grant = self.grant(agent_obj, repo=repo, task_files=task_files,
                           ttl_seconds=ttl_seconds)

        self.access.record_event(
            "job_started", agent_name=agent_obj["name"], token=grant["token"],
            task=task[:200], task_files=task_files or [],
            high_risk=disp.get("high_risk", False), session=cag.session_id)

        contract = {
            "task": task,
            "agent": agent_obj["name"],
            "agent_persona": agent_obj.get("body") or "",
            "agent_tools": agent_obj.get("tools") or [],
            "grant_token": grant["token"],
            "allowed_topics": grant["allowed_topics"],
            "can_write": grant["can_write"],
            "context_markdown": pack.get("markdown", ""),
            "context_files": pack.get("included_files", []),
            "session_id": cag.session_id,
            "repo": repo,
            "task_files": task_files or [],
        }

        # JOB 4 — RUN
        result = {"tool_calls": [], "edits": [], "decisions": [], "notes": []}
        status = "job_completed"
        if executor is not None:
            try:
                ex = executor(contract) or {}
                for k in ("tool_calls", "edits", "decisions", "notes"):
                    if isinstance(ex.get(k), list):
                        result[k] = ex[k]
            except Exception as e:
                status = "job_failed"
                result["error"] = f"{type(e).__name__}: {e}"

        # LOG — record the run into KERNEL-CAG (the hot loop)
        for tc in result["tool_calls"]:
            cag.append("tool_result", tc)
        for ed in result["edits"]:
            cag.append("edit", ed)
        for dc in result["decisions"]:
            cag.append("decision", dc)
        for nt in result["notes"]:
            cag.append("note", {"text": nt} if isinstance(nt, str) else nt)

        # WRITE-BACK to DG (structural + decisions) — best-effort
        writeback = self._writeback(result, repo)

        # DIGEST (close the session)
        digest = cag.close()
        self.access.record_event(
            status, agent_name=agent_obj["name"], token=grant["token"],
            session=cag.session_id, edited_files=digest["edited_files"],
            decision_count=digest["decision_count"],
            duration_s=round(time.time() - t0, 2))

        return {
            "status": status,
            "dispatch": {k: disp[k] for k in ("agent", "score", "high_risk", "reason", "candidates")},
            "context": {"files": pack.get("included_files", []),
                         "approx_tokens": pack.get("approx_tokens", 0)},
            "grant": {"token": grant["token"], "allowed_topics": grant["allowed_topics"],
                       "can_write": grant["can_write"], "expires_at": grant["expires_at"]},
            "result": result,
            "writeback": writeback,
            "digest": digest,
            "session_id": cag.session_id,
            "duration_s": round(time.time() - t0, 2),
        }

    # ── PHASE 4: CONSOLIDATE (the "sleep" step) ───────────────────────────
    def consolidate(self, run_dream: bool = True, archive: bool = True,
                    workspace=None) -> dict:
        """Drain ended KERNEL-CAG scratchpads into DG long-term memory, run the
        dream cycle, and archive the consumed scratchpads. Run periodically."""
        from .consolidate import consolidate as _consolidate
        return _consolidate(self.dg, self.storage_dir, run_dream=run_dream,
                            archive=archive, workspace=workspace)

    def _writeback(self, result: dict, repo: str) -> dict:
        """Push edits → code graph (update_files) and decisions → DG memory."""
        wb = {"update_files": None, "decisions_stored": 0}
        edits_with_text = [{"path": e["path"], "text": e["text"]}
                           for e in result.get("edits", [])
                           if e.get("path") and e.get("text")]
        if edits_with_text and repo:
            try:
                from . import code_graph as cg
                db = os.path.join(self.storage_dir, "code_graph.db")
                if os.path.exists(db):
                    wb["update_files"] = cg.update_files(db, edits_with_text, repo)
            except Exception as e:
                wb["update_files"] = {"error": str(e)}
        for dc in result.get("decisions", []):
            try:
                self.dg.memory.store(
                    question=str(dc.get("question", ""))[:500],
                    answer=str(dc.get("answer", ""))[:4000],
                    reasoning_summary=str(dc.get("reasoning", ""))[:2000],
                    communities_used=[], context_triples=[])
                wb["decisions_stored"] += 1
            except Exception:
                pass
        try:
            self.dg.memory.save()
        except Exception:
            pass
        return wb


# ── thin CLI (subphase 3.5) ────────────────────────────────────────────────
def _build_kernel(storage_dir: str, ws_root: str, agents_dir: str) -> "Kernel":
    from .core import DecisionGraph
    dg = DecisionGraph(storage_dir=storage_dir)
    return Kernel(dg, ws_root, agents_dir, storage_dir=storage_dir)


def main(argv=None) -> int:
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(
        prog="python -m decisiongraph.kernel",
        description="Kernel: run a task through DISPATCH → LOAD → GRANT → RUN+LOG.")
    ap.add_argument("task", nargs="?", default=None, help="the task description")
    ap.add_argument("--files", nargs="*", default=[], help="task file paths")
    ap.add_argument("--repo", default="", help="repo / blueprint name")
    ap.add_argument("--storage", default=os.environ.get("DG_STORAGE_DIR", "storage"))
    ap.add_argument("--ws-root", default=None, help="AgentNet workspace root")
    ap.add_argument("--agents-dir", default=os.environ.get("ECC_AGENTS_DIR", "ecc/agents"))
    ap.add_argument("--dispatch-only", action="store_true",
                    help="only run JOB 1 (pick agent), no grant/run")
    ap.add_argument("--consolidate", action="store_true",
                    help="Phase 4: run the sleep/consolidation pass and exit")
    ap.add_argument("--no-dream", action="store_true",
                    help="with --consolidate: skip the dream cycle")
    ap.add_argument("--token-budget", type=int, default=6000)
    args = ap.parse_args(argv)

    ws_root = args.ws_root or os.path.join(args.storage, "kernel_ws")
    os.makedirs(ws_root, exist_ok=True)
    k = _build_kernel(args.storage, ws_root, args.agents_dir)

    if args.consolidate:
        out = k.consolidate(run_dream=not args.no_dream)
        print(_json.dumps(out, indent=2))
        return 0

    if not args.task:
        ap.error("task is required unless --consolidate is given")

    if args.dispatch_only:
        out = k.dispatch(args.task, task_files=args.files)
        out.pop("_agent_obj", None)
        print(_json.dumps(out, indent=2))
        return 0

    # no executor on the CLI → safe dry-run (dispatch+load+grant, no edits)
    out = k.run_task(args.task, task_files=args.files, repo=args.repo,
                     token_budget=args.token_budget, executor=None)
    print(_json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
