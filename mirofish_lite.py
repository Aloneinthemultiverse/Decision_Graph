"""MiroFish Lite — a faithful, fast reimplementation of the MiroFish prediction engine.

Implements the EXACT same 11-endpoint HTTP API and 5-stage architecture as the
real MiroFish (https://github.com/666ghj/MiroFish), so DecisionGraph's
MiroFishClient talks to it unchanged. The difference: instead of running
hundreds of per-agent-per-round OASIS LLM calls (which take hours through a
throttled proxy), it collapses each persona's behaviour into ONE rich
"reaction-arc" call. ~10 LLM calls total → minutes, not hours.

Architecture parity with real MiroFish:
  1. Graph Building      → /api/graph/ontology/generate  (LLM: entity/edge types)
  2. Environment Setup   → /api/graph/build              (LLM: extract entities)
                         → /api/simulation/prepare       (LLM: OASIS personas)
  3. Simulation          → /api/simulation/start         (LLM: per-persona arc)
  4. Report Generation   → /api/report/generate          (LLM: synthesis)
  5. (Deep interaction omitted — not used by the DG integration)

Run:  python mirofish_lite.py     (listens on :5001 — drop-in for the real one)
"""
from __future__ import annotations
import os, sys, json, time, uuid, re, threading, tempfile
from pathlib import Path
from typing import Any
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse
import uvicorn

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# ── LLM client ─────────────────────────────────────────────────────────────────
# Default: local Ollama (OpenAI-compatible, no rate limit, no truncation).
# Override with MFLITE_LLM=anthropic to use DecisionGraph's saved ngrok creds.
import requests as _rq
import anthropic
_CREDS = ROOT / ".dg_credentials.json"

# Default: route through the DG credentials' Anthropic-format LLM gateway
# (e.g. local :8080 proxy → Gemini Flash). Falls back to Ollama if MFLITE_LLM
# is set to "ollama" explicitly. This keeps MiroFish lightweight on laptops —
# no large local-model load, fast inference, same gateway used everywhere else.
LLM_BACKEND = os.getenv("MFLITE_LLM", "anthropic")        # "anthropic" | "ollama"
OLLAMA_URL  = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")

def _load_anthropic():
    cfg = {}
    try:
        if _CREDS.exists(): cfg = json.loads(_CREDS.read_text())
    except Exception: pass
    return anthropic.Anthropic(
        api_key=cfg.get("api_key") or os.getenv("LLM_API_KEY", "test"),
        base_url=cfg.get("base_url") or os.getenv("LLM_BASE_URL", "https://api.anthropic.com"),
    ), (cfg.get("model") or os.getenv("LLM_MODEL", "claude-sonnet-4-5"))

if LLM_BACKEND == "anthropic":
    _ANTH_CLIENT, LLM_MODEL = _load_anthropic()
else:
    LLM_MODEL = OLLAMA_MODEL

def llm(prompt: str, max_tokens: int = 4000, json_mode: bool = False) -> str:
    """One LLM call → text. Routes to Ollama (default) or Anthropic.
    json_mode=True asks Ollama to constrain output to a valid JSON object."""
    if LLM_BACKEND == "anthropic":
        r = _ANTH_CLIENT.messages.create(
            model=LLM_MODEL, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in r.content
                        if b.type == "text" and getattr(b, "text", "")).strip()
        if not text:
            for b in r.content:
                if getattr(b, "type", "") == "thinking" and getattr(b, "thinking", ""):
                    text = b.thinking; break
        return text
    # Ollama (OpenAI-compatible chat completions)
    payload = {"model": LLM_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": 0.4}
    if json_mode:
        # Ollama honours response_format; forces syntactically valid JSON
        payload["response_format"] = {"type": "json_object"}
    resp = _rq.post(f"{OLLAMA_URL}/v1/chat/completions", json=payload, timeout=300)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _repair_json(s: str) -> str:
    """Best-effort cleanup of common small-model JSON defects."""
    s = s.strip()
    # strip code fences
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE | re.MULTILINE)
    # remove trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s.strip()

def llm_json(prompt: str, max_tokens: int = 4000, retries: int = 2) -> Any:
    """LLM call expecting JSON, with json-mode + repair + retry.
    Raises only after all retries fail."""
    full = prompt + "\n\nReturn ONLY valid JSON. No prose, no markdown fences."
    last_err = None
    for attempt in range(retries + 1):
        try:
            raw = llm(full, max_tokens, json_mode=True)
            m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
            candidate = m.group(1) if m else raw
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                return json.loads(_repair_json(candidate))
        except Exception as e:
            last_err = e
            full = (prompt + "\n\nYour previous output was invalid JSON. "
                    "Return STRICTLY valid minified JSON only.")
    raise ValueError(f"JSON parse failed after {retries+1} attempts: {last_err}")


# ── in-memory state ────────────────────────────────────────────────────────────
PROJECTS:    dict[str, dict] = {}
TASKS:       dict[str, dict] = {}   # graph-build tasks
SIMS:        dict[str, dict] = {}
REPORTS:     dict[str, dict] = {}
REPORT_TASKS:dict[str, dict] = {}

app = FastAPI(title="MiroFish Lite")

def ok(data):  return JSONResponse({"success": True, "data": data})
def err(msg, code=500): return JSONResponse({"success": False, "error": msg}, status_code=code)


@app.get("/health")
@app.get("/")
def health():
    return {"status": "ok", "service": "MiroFish Lite", "model": LLM_MODEL}


# ── STAGE 1: ontology generation ───────────────────────────────────────────────
@app.post("/api/graph/ontology/generate")
async def ontology_generate(
    files: list[UploadFile] = File(default=[]),
    simulation_requirement: str = Form(""),
    project_name: str = Form("project"),
    additional_context: str = Form(""),
):
    seed_text = ""
    for f in files:
        seed_text += (await f.read()).decode("utf-8", errors="ignore") + "\n"
    seed_text = (seed_text + "\n" + simulation_requirement).strip()
    if not seed_text:
        return err("empty seed material", 400)

    project_id = f"proj_{uuid.uuid4().hex[:12]}"
    try:
        ont = llm_json(
            "You are an ontology designer for a social-simulation engine. "
            "Given the decision/scenario below, design the entity and edge types "
            "for a knowledge graph of the stakeholders and forces involved.\n\n"
            f"SCENARIO:\n{seed_text[:4000]}\n\n"
            'Output JSON shaped exactly as: '
            '{"entity_types":[{"name":"...","description":"..."}],'
            '"edge_types":[{"name":"...","description":"..."}],'
            '"analysis_summary":"2-3 sentence summary"}',
            max_tokens=3000,
        )
    except Exception as e:
        return err(f"ontology LLM failure: {e}")

    PROJECTS[project_id] = {
        "project_id": project_id, "name": project_name,
        "seed_text": seed_text, "simulation_requirement": simulation_requirement,
        "ontology": ont, "graph_id": None,
    }
    return ok({
        "project_id": project_id,
        "ontology": ont,
        "files": [{"filename": f.filename} for f in files],
        "total_text_length": len(seed_text),
    })


# ── STAGE 2a: graph build (entity extraction) ──────────────────────────────────
@app.post("/api/graph/build")
async def graph_build(req: Request):
    body = await req.json()
    pid = body.get("project_id")
    if pid not in PROJECTS:
        return err("project not found", 404)
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    TASKS[task_id] = {"status": "running", "progress": 0, "result": {}}

    def _build():
        try:
            proj = PROJECTS[pid]
            ent_names = [e["name"] for e in proj["ontology"].get("entity_types", [])]
            TASKS[task_id]["progress"] = 40
            data = llm_json(
                "Extract the concrete stakeholders/entities for this scenario as a "
                "knowledge graph. For each, give a name and which ontology type it is.\n\n"
                f"SCENARIO:\n{proj['seed_text'][:3000]}\n\n"
                f"ONTOLOGY ENTITY TYPES: {ent_names}\n\n"
                'Output JSON: {"entities":[{"name":"...","type":"...","note":"one line"}]}',
                max_tokens=3000,
            )
            entities = data.get("entities", [])
            graph_id = f"mirofish_{uuid.uuid4().hex[:12]}"
            proj["graph_id"] = graph_id
            proj["entities"] = entities
            TASKS[task_id].update({"status": "completed", "progress": 100,
                                   "result": {"graph_id": graph_id,
                                              "entity_count": len(entities)}})
        except Exception as e:
            TASKS[task_id].update({"status": "failed", "error": str(e)})

    threading.Thread(target=_build, daemon=True).start()
    return ok({"task_id": task_id, "status": "running"})


@app.get("/api/graph/task/{task_id}")
def graph_task(task_id: str):
    t = TASKS.get(task_id)
    if not t: return err("task not found", 404)
    return ok(t)


# ── STAGE 2b: simulation create ────────────────────────────────────────────────
@app.post("/api/simulation/create")
async def sim_create(req: Request):
    body = await req.json()
    pid = body.get("project_id"); gid = body.get("graph_id")
    if pid not in PROJECTS:
        return err("project not found", 404)
    sid = f"sim_{uuid.uuid4().hex[:12]}"
    SIMS[sid] = {
        "simulation_id": sid, "project_id": pid, "graph_id": gid,
        "enable_twitter": body.get("enable_twitter", True),
        "enable_reddit":  body.get("enable_reddit", True),
        "prepare_status": "not_started", "prepare_progress": 0,
        "profiles": [], "run_status": "created",
        "current_round": 0, "total_rounds": 0, "progress_percent": 0.0,
        "persona_reactions": [],
    }
    return ok({"simulation_id": sid, "status": "created"})


# ── STAGE 2c: prepare (OASIS persona generation) ───────────────────────────────
@app.post("/api/simulation/prepare")
async def sim_prepare(req: Request):
    body = await req.json()
    sid = body.get("simulation_id")
    sim = SIMS.get(sid)
    if not sim: return err("simulation not found", 404)
    proj = PROJECTS[sim["project_id"]]
    entities = proj.get("entities", [])
    if not entities:
        return ok({"expected_entities_count": 0,
                   "message": "no entities in graph", "status": "failed"})

    sim["prepare_status"] = "preparing"

    def _prep():
        try:
            personas = llm_json(
                "Generate OASIS social-media personas for these stakeholders so we "
                "can simulate how they'd react to the decision. One persona per entity.\n\n"
                f"DECISION:\n{proj['seed_text'][:2000]}\n\n"
                f"ENTITIES: {json.dumps(entities)}\n\n"
                'Output JSON: {"personas":[{"name":"...","persona_type":"investor|customer|'
                'competitor|employee|media|regulator|analyst","background":"2 sentences on '
                'who they are and what they care about"}]}',
                max_tokens=4000,
            ).get("personas", [])
            for i, p in enumerate(personas):
                p["id"] = f"p{i}"
                sim["profiles"].append(p)
                sim["prepare_progress"] = int(100 * (i + 1) / max(len(personas), 1))
                time.sleep(0.05)  # let realtime poll observe them appearing
            sim["expected_entities_count"] = len(personas)
            sim["total_rounds"] = len(personas)   # 1 "round" per persona arc
            sim["prepare_status"] = "ready"
        except Exception as e:
            sim["prepare_status"] = "failed"
            sim["prepare_error"] = str(e)

    threading.Thread(target=_prep, daemon=True).start()
    # mirror real MiroFish: report expected count synchronously so DG's
    # fail-fast guard sees a non-zero number
    return ok({"expected_entities_count": len(entities),
               "status": "preparing",
               "task_id": uuid.uuid4().hex})


@app.post("/api/simulation/prepare/status")
async def sim_prepare_status(req: Request):
    body = await req.json()
    sim = SIMS.get(body.get("simulation_id"))
    if not sim: return err("simulation not found", 404)
    return ok({
        "status": sim["prepare_status"],
        "progress": sim["prepare_progress"],
        "expected_entities_count": sim.get("expected_entities_count", len(sim["profiles"])),
    })


@app.get("/api/simulation/{sid}/profiles/realtime")
def sim_profiles_realtime(sid: str):
    sim = SIMS.get(sid)
    if not sim: return err("simulation not found", 404)
    return ok({"profiles": sim["profiles"]})


# ── STAGE 3: simulation run (collapsed reaction arcs) ──────────────────────────
@app.post("/api/simulation/start")
async def sim_start(req: Request):
    body = await req.json()
    sim = SIMS.get(body.get("simulation_id"))
    if not sim: return err("simulation not found", 404)
    proj = PROJECTS[sim["project_id"]]
    sim["run_status"] = "running"

    # N discussion rounds — each round every persona reacts seeing the evolving
    # public conversation (like OASIS's Twitter/Reddit rounds, just compressed).
    # Default 1 for demo speed; bump MFLITE_ROUNDS for richer multi-round debate.
    ROUNDS = int(os.getenv("MFLITE_ROUNDS", "7"))

    def _persona_prompt(p, ptype, rnd, feed_ctx):
        return (
            f"You ARE {p['name']}, a {ptype}. {p.get('background','')}\n\n"
            f"DECISION YOU MUST JUDGE:\n{proj['seed_text'][:1800]}\n\n"
            f"PUBLIC REACTIONS SO FAR (round {rnd}/{ROUNDS}):\n{feed_ctx}\n\n"
            "Take a CLEAR, OPINIONATED position on THIS specific decision from "
            "your self-interest as this stakeholder. Do NOT describe yourself "
            "generically — argue FOR or AGAINST and say why it helps or hurts "
            "YOU. Pick a side: sentiment must be 'positive' (you support it), "
            "'negative' (you oppose/are threatened), or 'neutral' only if "
            "genuinely indifferent (rare — avoid).\n\n"
            'Output JSON: {"sentiment":"positive|negative|neutral",'
            '"post":"1-2 sentence punchy public post taking a side",'
            '"reaction":"3-4 sentences arguing your position",'
            '"day_1":"immediate move","week_1":"week 1 action",'
            '"month_1":"month 1 outlook","month_3":"month 3 outlook",'
            '"key_concern":"biggest worry","key_upside":"upside or none"}'
        )

    def _run():
        try:
            profiles = sim["profiles"]
            n = max(len(profiles), 1)
            sim["total_rounds"] = ROUNDS * n
            feed: list[str] = []
            done = 0
            for rnd in range(1, ROUNDS + 1):
                for p in profiles:
                    ptype = p.get("persona_type", "stakeholder")
                    feed_ctx = "\n".join(feed[-8:]) if feed else "(no public reactions yet)"
                    try:
                        arc = llm_json(_persona_prompt(p, ptype, rnd, feed_ctx),
                                       max_tokens=1200)
                    except Exception as e:
                        # one bad persona must not kill the whole simulation
                        print(f"  persona {p.get('name')} failed ({e}); using fallback")
                        arc = {"sentiment": "neutral",
                               "post": f"{p.get('name')} is still evaluating the decision.",
                               "reaction": f"As {ptype}, {p.get('name')} could not "
                                           f"reach a firm position this round.",
                               "day_1": "", "week_1": "", "month_1": "", "month_3": "",
                               "key_concern": "", "key_upside": ""}
                    arc["persona"] = ptype
                    arc["name"] = p["name"]
                    arc["round"] = rnd
                    sim["persona_reactions"].append(arc)
                    feed.append(f"[{p['name']} ({ptype})]: {arc.get('post','')}")
                    done += 1
                    sim["current_round"] = done
                    sim["progress_percent"] = round(100 * done / (ROUNDS * n), 1)
            sim["run_status"] = "completed"
        except Exception as e:
            import traceback; traceback.print_exc()
            sim["run_status"] = "failed"
            sim["run_error"] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return ok({"status": "running", "platform": "compressed"})


@app.get("/api/simulation/{sid}/run-status")
def sim_run_status(sid: str):
    sim = SIMS.get(sid)
    if not sim: return err("simulation not found", 404)
    return ok({
        "runner_status": sim["run_status"],
        "status": sim["run_status"],
        "progress_percent": sim["progress_percent"],
        "current_round": sim["current_round"],
        "total_rounds": sim["total_rounds"],
        "twitter_current_round": sim["current_round"],
        "reddit_current_round": sim["current_round"],
        "twitter_completed": sim["run_status"] == "completed",
        "reddit_completed": sim["run_status"] == "completed",
        "twitter_running": sim["run_status"] == "running",
        "reddit_running": sim["run_status"] == "running",
        "total_actions_count": len(sim["persona_reactions"]),
        "error": sim.get("run_error"),
    })


# ── STAGE 4: report generation (synthesis) ─────────────────────────────────────
@app.post("/api/report/generate")
async def report_generate(req: Request):
    body = await req.json()
    sid = body.get("simulation_id")
    sim = SIMS.get(sid)
    if not sim: return err("simulation not found", 404)

    report_id = f"report_{uuid.uuid4().hex[:12]}"
    task_id = uuid.uuid4().hex
    REPORT_TASKS[task_id] = {"status": "generating", "report_id": report_id}

    def _gen():
        try:
            proj = PROJECTS[sim["project_id"]]
            reactions = sim["persona_reactions"]
            # settled position = each persona's LAST round reaction
            final_by_name = {}
            for r in reactions:
                final_by_name[r.get("name")] = r
            settled = list(final_by_name.values())
            pos = sum(1 for r in settled if r.get("sentiment") == "positive")
            neg = sum(1 for r in settled if r.get("sentiment") == "negative")
            total = len(settled) or 1
            consensus = round(100 * pos / total)

            synth = llm_json(
                "Synthesise a decision-simulation report from these stakeholder "
                "reaction arcs.\n\n"
                f"DECISION:\n{proj['seed_text'][:1500]}\n\n"
                f"REACTIONS:\n{json.dumps(reactions)[:6000]}\n\n"
                'Output JSON: {"risks":["..."],"opportunities":["..."],'
                '"timeline":{"day_1":"aggregate day-1 reaction",'
                '"week_1":"...","month_1":"...","month_3":"..."},'
                '"by_persona":{"<persona_type>":"1-2 sentence summary"}}',
                max_tokens=3000,
            )
            REPORTS[report_id] = {
                "report_id": report_id, "simulation_id": sid,
                "consensus_score": consensus,
                "sentiment_breakdown": {"positive": pos, "negative": neg,
                                        "neutral": total - pos - neg, "total": total},
                "risks": synth.get("risks", [])[:6],
                "opportunities": synth.get("opportunities", [])[:6],
                "timeline": synth.get("timeline", {}),
                "by_persona": synth.get("by_persona", {}),
                "persona_reactions": reactions,
            }
            REPORT_TASKS[task_id].update({"status": "completed", "report_id": report_id})
        except Exception as e:
            REPORT_TASKS[task_id].update({"status": "failed", "error": str(e)})

    threading.Thread(target=_gen, daemon=True).start()
    return ok({"task_id": task_id, "report_id": report_id,
               "status": "generating", "already_generated": False})


@app.post("/api/report/generate/status")
async def report_status(req: Request):
    body = await req.json()
    t = REPORT_TASKS.get(body.get("task_id"))
    if not t: return err("report task not found", 404)
    return ok(t)


@app.get("/api/report/{report_id}")
def get_report(report_id: str):
    r = REPORTS.get(report_id)
    if not r: return err("report not found", 404)
    return ok(r)


def _warmup():
    """Pre-load the Ollama model so the first real request isn't a cold start."""
    if LLM_BACKEND != "ollama":
        return
    try:
        print(f"Warming up {LLM_MODEL} ...")
        # keep_alive=-1 keeps the model resident indefinitely
        _rq.post(f"{OLLAMA_URL}/api/generate",
                 json={"model": LLM_MODEL, "prompt": "ok", "stream": False,
                       "keep_alive": -1, "options": {"num_predict": 1}},
                 timeout=300)
        print("Warmup complete — model resident.")
    except Exception as e:
        print(f"Warmup failed (non-fatal): {e}")


if __name__ == "__main__":
    print(f"MiroFish Lite on :5001  (LLM={LLM_MODEL}, backend={LLM_BACKEND})")
    _warmup()
    uvicorn.run(app, host="0.0.0.0", port=5001, log_level="warning")
