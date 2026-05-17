"""Simulation Studio — multi-persona reaction simulator for major decisions.

Two execution modes:
  1. MiroFish (real)  — when `mirofish_url` is configured, runs the 11-step
     async pipeline against a MiroFish backend (https://github.com/666ghj/MiroFish)
  2. Local LLM (mock) — default fallback. Roleplays each user-selected persona
     via the existing LLM client so the studio works without external services.

Simulations run in a daemon thread; the frontend polls /api/simulation/{id}
for progress, agent feed, and the final report.
"""
from __future__ import annotations
import os
import uuid
import json
import re
import time
import tempfile
import threading
import requests
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Any, Callable

from . import config


# Personas used ONLY in local mock mode — MiroFish auto-generates personas from
# the seed material via its OASIS profile generator, so these checkboxes don't
# influence a MiroFish run.
PERSONA_PROMPTS = {
    "investors": (
        "You are a senior venture-capital investor reviewing this decision. "
        "You care about ROI, market timing, capital efficiency, dilution risk."
    ),
    "competitors": (
        "You are a senior strategist at a direct competitor. Consider how you would "
        "respond — counter-move, hold, or ignore — and what the second-order effects are."
    ),
    "customers": (
        "You are a target customer (decision-maker for purchases of this kind). "
        "Consider value, fit, switching costs, and how this changes your buying behaviour."
    ),
    "employees": (
        "You are a tenured employee at the company. Consider morale, workload, "
        "alignment with the company mission, and impact on your own role."
    ),
    "media": (
        "You are a senior tech/business journalist. Consider what makes this "
        "newsworthy, the angle of your story, who you would quote."
    ),
    "regulators": (
        "You are a regulator overseeing this industry. Consider compliance, "
        "public-welfare implications, and what precedent this sets."
    ),
}


@dataclass
class AgentResponse:
    persona: str
    name: str
    reaction: str
    sentiment: str            # positive | negative | neutral
    confidence: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class SimulationState:
    id: str
    decision_text: str
    personas: list[str]
    depth: int
    company_id: str
    mode: str = "local"       # "local" | "mirofish"
    status: str = "running"   # running | done | error
    stage:  str = "starting"  # human-readable current stage label
    progress: float = 0.0
    agent_responses: list = field(default_factory=list)
    report: dict = field(default_factory=dict)
    error: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    ended_at: str = ""
    # MiroFish-only IDs (populated as the pipeline progresses)
    mirofish: dict = field(default_factory=dict)   # {project_id, graph_id, simulation_id, report_id}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["agent_responses"] = [asdict(r) for r in self.agent_responses]
        return d


# ──────────────────────────────────────────────────────────────────────────────
# MiroFishClient — orchestrates the real 11-step pipeline
# ──────────────────────────────────────────────────────────────────────────────
class MiroFishClient:
    """Thin client over the MiroFish Flask backend. Each method makes one HTTP
    call; orchestration lives in `run_pipeline()`."""

    def __init__(self, base_url: str, timeout: int = 180):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    # ── individual endpoints ────────────────────────────────────────────────
    def health(self) -> dict:
        r = requests.get(f"{self.base}/health", timeout=10)
        r.raise_for_status()
        return r.json()

    def generate_ontology(self, seed_path: str, simulation_requirement: str,
                          project_name: str = "DecisionGraph", additional_context: str = "") -> dict:
        """Step 1 — multipart upload, returns {project_id, ontology, files, ...}"""
        with open(seed_path, "rb") as f:
            files = {"files": (os.path.basename(seed_path), f, "text/plain")}
            data = {
                "simulation_requirement": simulation_requirement,
                "project_name": project_name,
                "additional_context": additional_context,
            }
            r = requests.post(f"{self.base}/api/graph/ontology/generate",
                              files=files, data=data, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    def build_graph(self, project_id: str) -> str:
        """Step 2 — kick off async graph build → returns task_id"""
        r = requests.post(f"{self.base}/api/graph/build",
                          json={"project_id": project_id}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]["task_id"]

    def task_status(self, task_id: str) -> dict:
        r = requests.get(f"{self.base}/api/graph/task/{task_id}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    def create_simulation(self, project_id: str, graph_id: str,
                          enable_twitter: bool = True, enable_reddit: bool = True) -> str:
        body = {"project_id": project_id, "graph_id": graph_id,
                "enable_twitter": enable_twitter, "enable_reddit": enable_reddit}
        r = requests.post(f"{self.base}/api/simulation/create", json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]["simulation_id"]

    def prepare_simulation(self, simulation_id: str) -> dict:
        r = requests.post(f"{self.base}/api/simulation/prepare",
                          json={"simulation_id": simulation_id}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data", {})

    def prepare_status(self, simulation_id: str) -> dict:
        r = requests.post(f"{self.base}/api/simulation/prepare/status",
                          json={"simulation_id": simulation_id}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data", {})

    def start_simulation(self, simulation_id: str) -> dict:
        r = requests.post(f"{self.base}/api/simulation/start",
                          json={"simulation_id": simulation_id}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data", {})

    def run_status(self, simulation_id: str) -> dict:
        r = requests.get(f"{self.base}/api/simulation/{simulation_id}/run-status", timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data", {})

    def generate_report(self, simulation_id: str) -> dict:
        r = requests.post(f"{self.base}/api/report/generate",
                          json={"simulation_id": simulation_id}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    def report_generate_status(self, task_id: str) -> dict:
        r = requests.post(f"{self.base}/api/report/generate/status",
                          json={"task_id": task_id}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data", {})

    def get_report(self, report_id: str) -> dict:
        r = requests.get(f"{self.base}/api/report/{report_id}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    def simulation_profiles_realtime(self, simulation_id: str) -> dict:
        """OASIS personas generated during prepare — used to populate the agent feed."""
        r = requests.get(f"{self.base}/api/simulation/{simulation_id}/profiles/realtime",
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data", {})

    # ── orchestration ──────────────────────────────────────────────────────
    def run_pipeline(self, sim: SimulationState, on_stage: Callable[[str, float], None],
                     on_agent: Callable[[AgentResponse], None],
                     poll_interval: float = 2.0, max_wait_s: int = 5400) -> dict:
        # 90-minute ceiling — OASIS multi-agent rounds can take 10-30+ minutes
        # depending on persona count and platform breadth.
        """Drive all 11 steps. Updates `sim.stage`/`sim.progress`/`sim.mirofish`
        and emits agent responses via `on_agent` as personas are produced.
        Returns the final report dict."""
        deadline = time.time() + max_wait_s

        # 1 — seed file from the decision text
        on_stage("Uploading seed material", 0.05)
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        try:
            tmp.write(f"# Decision under evaluation\n\n{sim.decision_text}\n")
            tmp.close()
            ont = self.generate_ontology(
                seed_path=tmp.name,
                simulation_requirement=sim.decision_text,
                project_name=f"DG-{sim.id}",
            )
        finally:
            try: os.unlink(tmp.name)
            except Exception: pass

        project_id = ont["project_id"]
        sim.mirofish["project_id"] = project_id

        # 2 — kick off graph build
        on_stage("Building knowledge graph (this may take a few minutes)", 0.15)
        task_id = self.build_graph(project_id)

        # 3 — poll graph build
        graph_id = self._poll(
            fn=lambda: self.task_status(task_id),
            done=lambda d: d.get("status") in ("completed", "failed"),
            label="graph build", on_stage=on_stage,
            start_p=0.15, end_p=0.40, deadline=deadline, poll_interval=poll_interval,
        )
        if graph_id.get("status") == "failed":
            raise RuntimeError(f"Graph build failed: {graph_id.get('error') or 'unknown'}")
        graph_id_val = (graph_id.get("result") or {}).get("graph_id") or graph_id.get("graph_id")
        if not graph_id_val:
            raise RuntimeError("MiroFish: graph build returned no graph_id")
        sim.mirofish["graph_id"] = graph_id_val

        # 4 — create simulation
        on_stage("Creating simulation", 0.42)
        simulation_id = self.create_simulation(project_id, graph_id_val)
        sim.mirofish["simulation_id"] = simulation_id

        # 5 — prepare (generate OASIS personas)
        on_stage("Generating OASIS personas", 0.45)
        prep_response = self.prepare_simulation(simulation_id)

        # Fail-fast guard: if Zep graph has zero entities, MiroFish can't make personas.
        # Don't poll forever — bail with a clear message so the user can retry.
        if prep_response.get("expected_entities_count", -1) == 0:
            raise RuntimeError(
                "Zep extracted 0 entities from the seed material. The decision text "
                "may be too short or sparse for entity extraction. Try a richer "
                "description (mention stakeholders, products, markets, dollar amounts)."
            )

        # 6 — poll prepare; stream personas into agent feed as they appear
        already_seen = set()
        not_started_count = {"n": 0}
        def _on_prepare_tick():
            try:
                profiles = self.simulation_profiles_realtime(simulation_id).get("profiles") or []
                for p in profiles:
                    pid = p.get("id") or p.get("uuid") or p.get("name")
                    if pid in already_seen: continue
                    already_seen.add(pid)
                    on_agent(AgentResponse(
                        persona=p.get("persona_type") or p.get("type") or "persona",
                        name=p.get("name") or f"Agent {len(already_seen)}",
                        reaction=p.get("background") or p.get("description") or "Persona generated.",
                        sentiment="neutral",
                        confidence=0.7,
                    ))
            except Exception: pass

        def _prepare_done(d):
            if d.get("status") in ("ready", "completed", "failed"):
                return True
            # If status keeps reporting not_started after we called prepare,
            # MiroFish never picked up the work — bail after ~30 polls (~60s)
            if d.get("status") == "not_started":
                not_started_count["n"] += 1
                if not_started_count["n"] > 30:
                    raise RuntimeError(
                        "MiroFish prepare task never started (status stayed 'not_started'). "
                        "Likely an empty Zep graph — re-run with richer decision text."
                    )
            return False

        self._poll(
            fn=lambda: self.prepare_status(simulation_id),
            done=_prepare_done,
            label="OASIS prepare", on_stage=on_stage,
            start_p=0.45, end_p=0.65, deadline=deadline, poll_interval=poll_interval,
            tick=_on_prepare_tick,
        )

        # 7 — start
        on_stage("Running simulation rounds", 0.66)
        self.start_simulation(simulation_id)

        # 8 — poll run-status (with detailed round progress in the stage label)
        def _run_done(d):
            # MiroFish's run-status uses runner_status, not status
            rs = d.get("runner_status") or d.get("status") or ""
            return rs in ("completed", "finished", "failed", "stopped")
        def _run_stage_msg(d):
            cur = d.get("current_round", 0)
            total = d.get("total_rounds", 0) or 1
            tw = d.get("twitter_current_round", 0)
            rd = d.get("reddit_current_round", 0)
            acts = d.get("total_actions_count", 0)
            return f"round {cur}/{total} (Twitter {tw}, Reddit {rd}, {acts} actions)"
        # custom inline poll so we can show MiroFish's own progress_percent
        last_msg = "simulation run"
        while True:
            if time.time() > deadline:
                raise TimeoutError("MiroFish: simulation run exceeded max_wait_s")
            try:
                data = self.run_status(simulation_id)
                pct_inner = float(data.get("progress_percent", 0.0)) / 100.0
                progress_outer = 0.66 + 0.19 * max(0.0, min(1.0, pct_inner))
                last_msg = _run_stage_msg(data)
                on_stage(f"simulation run — {last_msg}", progress_outer)
                if _run_done(data):
                    run = data
                    break
            except requests.HTTPError as e:
                last_msg = f"simulation run (transient {e.response.status_code})"
            except Exception as e:
                last_msg = f"simulation run ({e})"
            time.sleep(poll_interval)
        if run.get("status") == "failed":
            raise RuntimeError(f"Simulation run failed: {run.get('error') or 'unknown'}")

        # 9 — generate report
        on_stage("Generating analysis report", 0.86)
        rg = self.generate_report(simulation_id)
        # report task may be sync (if already cached) or async
        if rg.get("already_generated"):
            report_id = rg["report_id"]
        else:
            rtask = rg.get("task_id")
            if not rtask: raise RuntimeError("MiroFish: report.generate returned no task_id")
            sim.mirofish["report_task_id"] = rtask
            # 10 — poll report status
            final = self._poll(
                fn=lambda: self.report_generate_status(rtask),
                done=lambda d: d.get("status") in ("completed", "finished", "failed"),
                label="report generation", on_stage=on_stage,
                start_p=0.86, end_p=0.97, deadline=deadline, poll_interval=poll_interval,
            )
            if final.get("status") == "failed":
                raise RuntimeError(f"Report generation failed: {final.get('error') or 'unknown'}")
            report_id = final.get("report_id") or rg.get("report_id")
        sim.mirofish["report_id"] = report_id

        # 11 — fetch final report
        on_stage("Fetching report", 0.99)
        report = self.get_report(report_id)

        # Replace the placeholder persona-profile cards with the REAL
        # per-round reactions (which carry actual sentiment + opinion text).
        reactions = report.get("persona_reactions") or []
        if reactions:
            sim.agent_responses.clear()
            for r in reactions:
                on_agent(AgentResponse(
                    persona=r.get("persona", "stakeholder"),
                    name=(f"{r.get('name','Agent')}"
                          + (f" · R{r['round']}" if r.get("round") else "")),
                    reaction=r.get("reaction") or r.get("post") or "",
                    sentiment=r.get("sentiment", "neutral"),
                    confidence=0.85,
                ))
        return report

    # ── poll helper ────────────────────────────────────────────────────────
    def _poll(self, *, fn, done, label, on_stage, start_p, end_p, deadline,
              poll_interval: float, tick: Callable[[], None] = None) -> dict:
        last_msg = label
        ticks = 0
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"MiroFish: {label} exceeded max_wait_s")
            try:
                data = fn()
                progress_inner = float(data.get("progress", 0.0)) / 100.0 if data.get("progress", 0) > 1 else float(data.get("progress", 0.0))
                progress_outer = start_p + (end_p - start_p) * max(0.0, min(1.0, progress_inner))
                msg = data.get("message") or data.get("stage") or last_msg
                on_stage(f"{label} — {msg}", progress_outer)
                if tick:
                    try: tick()
                    except Exception: pass
                if done(data):
                    return data
            except requests.HTTPError as e:
                # non-fatal, keep polling
                last_msg = f"{label} (transient {e.response.status_code})"
            except Exception as e:
                last_msg = f"{label} ({e})"
            ticks += 1
            time.sleep(poll_interval)


# ──────────────────────────────────────────────────────────────────────────────
class SimulationManager:
    """Holds active + completed simulations in-process."""

    def __init__(self):
        self.sims: dict[str, SimulationState] = {}
        self._lock = threading.Lock()

    def start(self, *, decision_text: str, personas: list[str], depth: int,
              company_context: str = "", company_id: str = "",
              client, embed_model=None, mirofish_url: str = "") -> str:
        if not decision_text.strip():
            raise ValueError("decision_text is required")
        if not personas:
            personas = list(PERSONA_PROMPTS.keys())
        depth = max(1, min(int(depth or 10), 100))
        mode = "mirofish" if mirofish_url else "local"
        sim = SimulationState(
            id=str(uuid.uuid4())[:8],
            decision_text=decision_text.strip(),
            personas=personas,
            depth=depth,
            company_id=company_id or "",
            mode=mode,
            stage="starting…",
        )
        with self._lock:
            self.sims[sim.id] = sim
        t = threading.Thread(
            target=self._run,
            args=(sim, company_context, client, embed_model, mirofish_url),
            daemon=True,
        )
        t.start()
        return sim.id

    def get(self, sim_id: str) -> Optional[SimulationState]:
        return self.sims.get(sim_id)

    def list_all(self) -> list[SimulationState]:
        return list(self.sims.values())

    # ── internal ──────────────────────────────────────────────────────────
    def _set_stage(self, sim: SimulationState, label: str, p: float):
        sim.stage = label
        sim.progress = max(0.0, min(1.0, round(p, 3)))

    def _run(self, sim: SimulationState, company_context: str, client,
             embed_model, mirofish_url: str) -> None:
        try:
            if mirofish_url:
                self._run_via_mirofish(sim, mirofish_url, client)
            else:
                self._run_locally(sim, company_context, client)
            self._build_report(sim, client)
            sim.status = "done"
            sim.stage = "complete"
            sim.progress = 1.0
            sim.ended_at = datetime.now().isoformat()
        except Exception as e:
            import traceback; traceback.print_exc()
            sim.status = "error"
            sim.error = str(e)
            sim.stage = f"error: {e}"
            sim.ended_at = datetime.now().isoformat()

    def _run_locally(self, sim: SimulationState, company_context: str, client) -> None:
        """Generate one persona reaction per (round, persona)."""
        self._set_stage(sim, "local LLM — running rounds", 0.02)
        total_steps = sim.depth * len(sim.personas)
        if total_steps == 0: return
        step = 0
        for round_num in range(1, sim.depth + 1):
            for persona in sim.personas:
                resp = self._llm_persona_response(persona, round_num, sim.decision_text,
                                                   company_context, client)
                sim.agent_responses.append(resp)
                step += 1
                self._set_stage(sim, f"local LLM — round {round_num}/{sim.depth}", step / total_steps * 0.9)

    def _llm_persona_response(self, persona: str, round_num: int, decision: str,
                              company_context: str, client) -> AgentResponse:
        instructions = PERSONA_PROMPTS.get(persona, f"You are a {persona}.")
        ctx_block = f"COMPANY CONTEXT:\n{company_context[:1500]}\n\n" if company_context else ""
        prompt = (
            f"{instructions}\n\n{ctx_block}"
            f"DECISION TO REACT TO:\n{decision}\n\n"
            f"You are persona #{round_num}, a unique individual in this group "
            f"(slightly different perspective from peers). React in 2-3 sentences. "
            f"End with EXACTLY one line:\nSentiment: <positive|negative|neutral>"
        )
        try:
            response = client.messages.create(
                model=config.LLM_MODEL,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = next((b.text.strip() for b in response.content if b.type == "text" and b.text.strip()), "")
        except Exception as e:
            return AgentResponse(
                persona=persona, name=f"{persona.title()} #{round_num}",
                reaction=f"(LLM error: {e})", sentiment="neutral", confidence=0.0,
            )

        sentiment = "neutral"
        m = re.search(r"sentiment\s*:\s*(positive|negative|neutral)", text, re.IGNORECASE)
        if m: sentiment = m.group(1).lower()
        reaction = re.split(r"sentiment\s*:", text, flags=re.IGNORECASE)[0].strip()
        return AgentResponse(
            persona=persona,
            name=f"{persona.title()} #{round_num}",
            reaction=reaction[:600],
            sentiment=sentiment,
            confidence=0.8,
        )

    def _run_via_mirofish(self, sim: SimulationState, mirofish_url: str, client) -> None:
        """Drive the full 11-step MiroFish pipeline."""
        cli = MiroFishClient(mirofish_url)
        try:
            cli.health()
        except Exception as e:
            raise RuntimeError(f"MiroFish health check failed at {mirofish_url}: {e}")

        def on_stage(label, p):  self._set_stage(sim, label, p)
        def on_agent(resp):       sim.agent_responses.append(resp)

        report = cli.run_pipeline(sim, on_stage=on_stage, on_agent=on_agent)
        # store full MiroFish report so /api/simulation/{id} returns it verbatim
        sim.report["mirofish_raw"] = report

    def _build_report(self, sim: SimulationState, client) -> None:
        """Aggregate sentiment + (for local mode) ask LLM for risks/opportunities/timeline.
        For MiroFish mode, prefer fields from the real report when present."""
        responses = sim.agent_responses
        pos = sum(1 for r in responses if r.sentiment == "positive")
        neg = sum(1 for r in responses if r.sentiment == "negative")
        total = len(responses) or 1
        consensus_score = round(100 * pos / total) if total else 0

        # per-persona aggregation
        by_persona_counts: dict[str, dict[str, int]] = {}
        for r in responses:
            b = by_persona_counts.setdefault(r.persona, {"positive": 0, "negative": 0, "neutral": 0})
            b[r.sentiment] = b.get(r.sentiment, 0) + 1

        risks, opps, timeline, by_persona = [], [], {}, {}

        # If MiroFish gave us a real report, TRUST its fields — its sentiment
        # comes from the actual multi-round simulation, not the feed cards.
        raw = (sim.report or {}).get("mirofish_raw") or {}
        if raw:
            risks    = raw.get("risks") or raw.get("risk_flags") or []
            opps     = raw.get("opportunities") or []
            timeline = raw.get("timeline") or {}
            by_persona = raw.get("by_persona") or raw.get("persona_analysis") or {}
            if "consensus_score" in raw:
                try: consensus_score = int(raw["consensus_score"])
                except Exception: pass
            # prefer MiroFish's own sentiment breakdown (real round sentiment)
            rsb = raw.get("sentiment_breakdown")
            if isinstance(rsb, dict) and rsb.get("total"):
                pos = rsb.get("positive", pos)
                neg = rsb.get("negative", neg)
                total = rsb.get("total", total)

        # If we still lack any of risks/opps/timeline, fall back to LLM aggregation
        if not (risks or opps or timeline):
            sample_size = min(30, len(responses))
            if sample_size:
                sample = "\n".join(f"[{r.persona}] {r.reaction}" for r in responses[:sample_size])
                analysis_prompt = (
                    f"DECISION:\n{sim.decision_text}\n\n"
                    f"STAKEHOLDER REACTIONS (sample of {sample_size}):\n{sample}\n\n"
                    "Produce a structured JSON analysis with these keys:\n"
                    "  risks:         [up to 5 short risk-flag strings]\n"
                    "  opportunities: [up to 5 short opportunity strings]\n"
                    "  timeline:      {day_1, week_1, month_1, month_3 — one short prediction each}\n"
                    "  by_persona:    {persona_name: 1-2 sentence summary, ...}\n"
                    "Return ONLY the JSON object, nothing else."
                )
                try:
                    response = client.messages.create(
                        model=config.LLM_MODEL, max_tokens=2000,
                        messages=[{"role": "user", "content": analysis_prompt}],
                    )
                    text = next((b.text for b in response.content if b.type == "text" and b.text.strip()), "{}")
                    m = re.search(r"\{[\s\S]*\}", text)
                    if m:
                        parsed = json.loads(m.group(0))
                        risks      = risks      or parsed.get("risks", [])[:5]
                        opps       = opps       or parsed.get("opportunities", [])[:5]
                        timeline   = timeline   or parsed.get("timeline", {})
                        by_persona = by_persona or parsed.get("by_persona", {})
                except Exception as e:
                    print(f"  Report LLM parse failed: {e}")

        sim.report.update({
            "consensus_score": consensus_score,
            "sentiment_breakdown": {
                "positive": pos, "negative": neg, "neutral": total - pos - neg, "total": total,
            },
            "by_persona_counts": by_persona_counts,
            "risks":          risks,
            "opportunities":  opps,
            "timeline":       timeline or {"day_1": "", "week_1": "", "month_1": "", "month_3": ""},
            "by_persona":     by_persona,
        })
