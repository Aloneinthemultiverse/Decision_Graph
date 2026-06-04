"""DecisionGraph — FastAPI backend serving the Stitch UI directly.
Replaces the Streamlit app entirely.
"""
from __future__ import annotations
import os, sys, json, time, tempfile, traceback
from pathlib import Path
from typing import Optional, Any
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import threading

ROOT      = Path(__file__).parent
STITCH    = ROOT / "stitch_ui"
CREDS     = ROOT / ".dg_credentials.json"
SETTINGS_PATH = ROOT / "storage" / "dg_settings.json"

DEFAULT_SETTINGS = {
    "uncertainty_gate_enabled": False,    # OFF at project start — graphs are sparse
    "uncertainty_threshold":    0.3,
    "mirofish_url":             "",       # blank → local mock mode for Simulation Studio
    "public_url":               "",       # public tunnel base URL (Cloudflare) for remote MCP
    "gemini_api_key":           "",       # server-side Google key for media transcription
}

sys.path.insert(0, str(ROOT))

# ──────────────────────────────────────────────────────────────────────────────
# Per-request workspace resolution
# ──────────────────────────────────────────────────────────────────────────────
import contextvars
from decisiongraph.workspace import WorkspaceManager

_CURRENT_WS: contextvars.ContextVar = contextvars.ContextVar("current_ws", default=None)
WSM = WorkspaceManager()


class _State:
    """`S.dg/hub/dm` resolve to the CURRENT request's isolated workspace.
    Everything else (settings, integrations, simulator) is process-shared.
    The server owns the LLM connection — visitors can't see or change it."""
    ready: bool = True            # server-managed LLM; always "connected"
    creds: dict = {}
    integrations: Any = None
    simulator: Any = None
    settings: dict = {}

    @property
    def ws(self):
        ws = _CURRENT_WS.get()
        if ws is None:
            raise HTTPException(status_code=500, detail="no workspace bound to request")
        return ws

    @property
    def dg(self):  return self.ws.dg
    @property
    def hub(self): return self.ws.hub
    @property
    def dm(self):  return self.ws.dm


S = _State()


def load_settings() -> dict:
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text())
            return {**DEFAULT_SETTINGS, **data}
    except Exception: pass
    return dict(DEFAULT_SETTINGS)

def save_settings(s: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(s, indent=2))
    except Exception: pass

S.settings = load_settings()


# integrations are independent of LLM connection — initialize once
from decisiongraph.integrations import IntegrationManager
S.integrations = IntegrationManager()

# Simulation Studio — transient in-process state
from decisiongraph.simulation import SimulationManager
S.simulator = SimulationManager()

# gbrain #6 — durable, restart-safe job queue
from decisiongraph.jobs import JobQueue
from decisiongraph.dream import run_dream_cycle
JOBS = JobQueue(os.path.join(str(ROOT), "storage", "jobs.db"))

def _job_dream(payload: dict) -> dict:
    """Runs in the queue worker (no request context) — resolve ws by token."""
    tok = payload.get("workspace")
    ws = WSM.get(tok)
    return run_dream_cycle(ws,
                           compile_topics=payload.get("compile_topics", True),
                           dedupe=payload.get("dedupe", True))

def _job_ingest(payload: dict) -> dict:
    """Async web/media ingest — runs in the queue worker so it never hits the
    Cloudflare ~100s tunnel timeout, and is restart-safe."""
    from decisiongraph.ingest_sources import (
        fetch_url_text, fetch_youtube_transcript,
        transcribe_media_gemini, text_to_tempfile)
    tok = payload["workspace"]
    ws = WSM.get(tok)
    company_id = payload.get("company_id")
    if company_id:
        cm = ws.hub.get_company(company_id)
        if not cm: return {"ok": False, "error": f"company {company_id} not found"}
        ingest_fn, stats_fn = cm.ingest, cm.stats
    else:
        ingest_fn, stats_fn = ws.dg.ingest, ws.dg.stats

    kind = payload["kind"]
    if kind == "pdf":
        # Direct file path; bypass the URL/youtube/media text-extract pipeline
        # and let the real ingest pipeline (read_document -> triples -> graph)
        # handle it. Same as POST /api/ingest/personal but async.
        path = payload["path"]
        if not os.path.isfile(path):
            return {"ok": False, "error": f"file not found: {path}"}
        try:
            with ws.lock:
                ingest_fn(path)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "path": path, "stats": stats_fn()}
    if kind == "url":
        title, text = fetch_url_text(payload["url"])
    elif kind == "youtube":
        title, text = fetch_youtube_transcript(payload["url"])
    elif kind == "media":
        title, text = transcribe_media_gemini(payload["media_path"],
                                              payload.get("gemini_key", ""))
    else:
        return {"ok": False, "error": f"unknown kind {kind}"}

    tmp = text_to_tempfile(text)
    try:
        with ws.lock:                  # serialise with this workspace's requests
            ingest_fn(tmp)
    except Exception as e:
        return {"ok": False, "error": f"too little extractable knowledge ({type(e).__name__})"}
    finally:
        try: os.unlink(tmp)
        except Exception: pass
        if kind == "media":
            try: os.unlink(payload["media_path"])
            except Exception: pass
    return {"ok": True, "title": title, "stats": stats_fn()}

def _job_ingest_github(payload: dict) -> dict:
    """Async GitHub repo ingest — NEW path. Builds a structured blueprint of
    the repo (project mission + concepts + file roles + function descriptions
    + commits + PRs) as ONE markdown document, then feeds it through DG's
    existing ingest pipeline. DG handles triples, Louvain communities, and
    compiled summaries — same machinery as PDF ingest. No parallel system."""
    from decisiongraph.codebase import ingest_github_url_v2, _parse_github_url
    tok = payload["workspace"]
    ws = WSM.get(tok)
    repo_url = payload["repo_url"]
    if not _parse_github_url(repo_url):
        return {"ok": False, "error": "must be a public github.com URL"}
    try:
        with ws.lock:
            stats = ingest_github_url_v2(
                ws.dg, repo_url,
                branch=(payload.get("branch") or None))
        if "error" in stats:
            return {"ok": False, "error": stats["error"], "stats": stats}
        return {"ok": True, "stats": stats}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

JOBS.register("dream", _job_dream)
JOBS.register("ingest", _job_ingest)
JOBS.register("ingest_github", _job_ingest_github)
JOBS.start()


def _auto_broadcast_decision_async(decision: dict):
    """Fire-and-forget broadcast so the user response isn't blocked."""
    def _run():
        try:
            r = S.integrations.broadcast_decision(decision, auto_only=True)
            if r: print(f"[auto-broadcast/decision] {decision.get('id')} → {list(r.keys())}")
        except Exception as e:
            print(f"[auto-broadcast/decision] error: {e}")
    threading.Thread(target=_run, daemon=True).start()


def _auto_broadcast_session_async(session: dict):
    def _run():
        try:
            r = S.integrations.broadcast_session(session, auto_only=True)
            if r: print(f"[auto-broadcast/session] {session.get('id')} → {list(r.keys())}")
        except Exception as e:
            print(f"[auto-broadcast/session] error: {e}")
    threading.Thread(target=_run, daemon=True).start()

def save_creds(c: dict) -> None:
    try: CREDS.write_text(json.dumps(c))
    except Exception: pass

def load_creds() -> Optional[dict]:
    try:
        if CREDS.exists(): return json.loads(CREDS.read_text())
    except Exception: pass
    return None

def do_connect(api_key: str, base_url: str, model: str) -> tuple[bool, str]:
    """Server-side LLM configuration. Sets the process-wide LLM config that
    every workspace's lazily-built DecisionGraph reads. Does NOT build any
    global graph instances — those are per-workspace and isolated."""
    try:
        if api_key:  os.environ["LLM_API_KEY"]  = api_key
        if base_url: os.environ["LLM_BASE_URL"] = base_url
        if model:    os.environ["LLM_MODEL"]    = model
        import decisiongraph.config as cfg
        if api_key:  cfg.LLM_API_KEY  = api_key
        if base_url: cfg.LLM_BASE_URL = base_url
        if model:    cfg.LLM_MODEL    = model
        S.ready = True
        S.creds = {"api_key": api_key, "base_url": base_url, "model": model}
        save_creds(S.creds)
        return True, ""
    except Exception as e:
        S.ready = False
        return False, str(e)

# auto-connect on startup
_c = load_creds()
if _c and _c.get("api_key"):
    ok, err = do_connect(_c["api_key"], _c.get("base_url", ""), _c.get("model", "claude-sonnet-4-5"))
    print(f"Auto-connect: {'OK' if ok else 'FAILED: ' + err}")

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="DecisionGraph",
              docs_url="/swagger",   # move FastAPI's auto-docs off /docs
              redoc_url="/redoc")    # our /docs is the human-readable guide
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

WS_COOKIE = "dg_ws"
DEVICE_COOKIE = "dg_device"
PROJECT_COOKIE = "dg_project"

# Paths that must NOT mint/require a workspace (static + health)
_NO_WS_PREFIXES = ("/favicon", "/static")

# Paths whose URL contains an explicit project id (e.g. /p/<pid>/app)
# are detected at request time below — no static list.

@app.middleware("http")
async def bind_workspace(request: Request, call_next):
    """Resolve this visitor's isolated workspace.

    Resolution order:
      1. URL-path projects:  /p/<project_id>/...    (cleanest, sharable)
      2. dg_project cookie + dg_device cookie       (in-browser switcher)
      3. dg_ws cookie                               (legacy — gets adopted)
      4. nothing — mint a fresh device + Default Project

    Existing dg_ws cookies are auto-adopted into a "Default Project" record
    so nobody's data ever disappears under them.
    """
    path = request.url.path
    if path in ("/favicon.ico",) or any(path.startswith(p) for p in _NO_WS_PREFIXES):
        return await call_next(request)

    from decisiongraph import projects as _proj

    # 1. URL-prefix project (/p/<pid>/...)
    requested_pid = None
    url_strip = ""
    if path.startswith("/p/"):
        rest = path[len("/p/"):]
        slash = rest.find("/")
        if slash > 0:
            requested_pid = rest[:slash]
            url_strip = "/p/" + requested_pid    # for later URL rewrite if needed

    # 2 + 3 + 4. cookies
    device_id = request.cookies.get(DEVICE_COOKIE)
    if not requested_pid:
        requested_pid = request.cookies.get(PROJECT_COOKIE)
    legacy_ws = (request.headers.get("X-Workspace")
                 or request.query_params.get("ws")
                 or request.cookies.get(WS_COOKIE))

    resolved = _proj.resolve_active_project(
        device_id=device_id,
        requested_project_id=requested_pid,
        ws_cookie=legacy_ws,
        wsm=WSM)

    device_id = resolved["device_id"]
    project = resolved["project"]
    token = project["ws_token"]
    minted = bool(resolved.get("created"))

    try:
        ws = WSM.get(token)
    except Exception:
        token = WorkspaceManager.new_token(); minted = True
        ws = WSM.get(token)

    # NOTE: do NOT hold ws.lock here. Holding a blocking threading lock across
    # `await call_next` stalls uvicorn's event loop whenever a background job
    # (durable queue) holds the same workspace lock for a long ingest — that
    # wedges the whole server. Isolation does not need this lock: every
    # workspace already has its own dg/hub/dm objects + storage dirs. The
    # job worker still takes ws.lock around its own writes, which is the only
    # place a real concurrent-write race can occur.
    tok = _CURRENT_WS.set(ws)
    try:
        response = await call_next(request)
    finally:
        _CURRENT_WS.reset(tok)

    # persist the workspace cookie so the visitor keeps their own world
    if minted or request.cookies.get(WS_COOKIE) != token:
        response.set_cookie(
            WS_COOKIE, token, max_age=60 * 60 * 24 * 30,
            httponly=True, samesite="lax",
        )
    # persist device + project cookies (project = the namespace; device = the
    # owner of multiple namespaces). Both long-lived.
    if request.cookies.get(DEVICE_COOKIE) != device_id:
        response.set_cookie(
            DEVICE_COOKIE, device_id, max_age=60 * 60 * 24 * 365,
            httponly=True, samesite="lax",
        )
    if request.cookies.get(PROJECT_COOKIE) != project["id"]:
        response.set_cookie(
            PROJECT_COOKIE, project["id"], max_age=60 * 60 * 24 * 365,
            httponly=False,   # readable by frontend JS so the dropdown can show current project
            samesite="lax",
        )
    return response


# ── rate limiting (per-workspace sliding window + global circuit breaker) ──────
_GLOBAL_LLM_HITS: list[float] = []
_GLOBAL_LLM_LOCK = threading.Lock()
GLOBAL_LLM_PER_MIN = 90          # protects the shared LLM/proxy across everyone

def rate_limit(bucket: str, max_n: int, window_s: int):
    """Per-workspace sliding-window limiter. Raises 429 when exceeded."""
    ws = _CURRENT_WS.get()
    if ws is None:
        return
    now = time.time()
    hits = ws.rl.setdefault(bucket, [])
    cutoff = now - window_s
    hits[:] = [t for t in hits if t > cutoff]
    if len(hits) >= max_n:
        raise HTTPException(status_code=429,
            detail=f"Rate limit: max {max_n} '{bucket}' per {window_s}s for this workspace. Slow down.")
    hits.append(now)

def global_llm_gate():
    """Process-wide circuit breaker so a burst can't drain the shared LLM."""
    now = time.time()
    with _GLOBAL_LLM_LOCK:
        _GLOBAL_LLM_HITS[:] = [t for t in _GLOBAL_LLM_HITS if t > now - 60]
        if len(_GLOBAL_LLM_HITS) >= GLOBAL_LLM_PER_MIN:
            raise HTTPException(status_code=503,
                detail="Server busy (global LLM limit). Try again shortly.")
        _GLOBAL_LLM_HITS.append(now)

def require_ready():
    if not S.ready:
        raise HTTPException(status_code=503, detail="LLM not configured on server.")

# ── page routes (serve the Stitch HTML) ───────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def home():
    # Stitch landing page is the front door; the app SPA lives at /app
    lp = STITCH / "landing.html"
    return FileResponse(lp if lp.exists() else STITCH / "index.html")

@app.get("/app", response_class=HTMLResponse)
def app_spa():
    return FileResponse(STITCH / "index.html")

@app.get("/favicon.ico")
def favicon():
    return JSONResponse(status_code=204, content=None)

# ──────────────────────────────────────────────────────────────────────────────
# API ENDPOINTS
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    if not S.ready:
        return {"ready": False, "creds": load_creds() or {}, "settings": S.settings}
    try:
        stats = S.dg.stats()
        return {
            "ready": True,
            "stats": stats,
            "creds": {"base_url": S.creds.get("base_url",""), "model": S.creds.get("model","")},
            "companies": len(S.hub.companies),
            "sessions":  len(S.dm.list_sessions()),
            "settings":  S.settings,
        }
    except Exception as e:
        return {"ready": True, "error": str(e), "settings": S.settings,
                "stats": {"nodes":0,"edges":0,"communities":0,"decisions":0}}

@app.get("/api/workspace")
def api_workspace():
    """Identity of THIS visitor's isolated workspace (for the UI badge)."""
    ws = _CURRENT_WS.get()
    return {
        "token": ws.token if ws else None,
        "short": (ws.token[:8] if ws else None),
        "created_at": ws.created_at if ws else None,
        "manager": WSM.stats(),
    }

@app.get("/api/settings")
def get_settings():
    # never expose secret/admin-only fields to visitors
    _HIDDEN = {"public_url", "gemini_api_key"}
    safe = {k: v for k, v in S.settings.items() if k not in _HIDDEN}
    safe["public_url"] = S.settings.get("public_url", "")  # display-only, harmless
    safe["media_ingest_enabled"] = bool(S.settings.get("gemini_api_key"))
    return {"settings": safe}

ADMIN_TOKEN = os.getenv("DG_ADMIN_TOKEN", "")

@app.post("/api/settings")
async def update_settings(req: Request):
    # Global server settings are operator-only. Set DG_ADMIN_TOKEN on the
    # server and pass it as X-Admin-Token; visitors cannot change global config.
    if not ADMIN_TOKEN or req.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Settings are server-managed.")
    body = await req.json()
    # accept only known keys, validate types
    if "uncertainty_gate_enabled" in body:
        S.settings["uncertainty_gate_enabled"] = bool(body["uncertainty_gate_enabled"])
    if "uncertainty_threshold" in body:
        try:
            v = float(body["uncertainty_threshold"])
            S.settings["uncertainty_threshold"] = max(0.0, min(1.0, v))
        except Exception: pass
    if "mirofish_url" in body:
        S.settings["mirofish_url"] = str(body["mirofish_url"] or "").strip()
    if "public_url" in body:
        S.settings["public_url"] = str(body["public_url"] or "").strip().rstrip("/")
    if "gemini_api_key" in body:
        S.settings["gemini_api_key"] = str(body["gemini_api_key"] or "").strip()
    save_settings(S.settings)
    return {"ok": True, "settings": S.settings}


# ──────────────────────────────────────────────────────────────────────────────
# SIMULATION STUDIO API
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/api/simulation/run")
async def simulation_run(req: Request):
    """Kick off a simulation. Returns the sim_id immediately; poll
    /api/simulation/{id} for progress + the final report."""
    require_ready()
    rate_limit("simulation", max_n=3, window_s=3600)   # 3 sims/hour/workspace
    global_llm_gate()
    body = await req.json()
    decision    = (body.get("decision_text") or body.get("decision") or "").strip()
    personas    = body.get("personas") or ["investors","competitors","customers","employees","media","regulators"]
    depth       = int(body.get("depth") or 10)
    company_id  = (body.get("company_id") or "").strip()

    if not decision:
        raise HTTPException(400, "decision_text is required")

    # Pull company context from the personal or per-company knowledge graph
    company_context = ""
    if company_id:
        cm = S.hub.get_company(company_id)
        if cm:
            cats = ", ".join(f"{k}={v}" for k, v in (cm.stats().get("categories") or {}).items())
            company_context = (f"Company: {cm.company_name}\n"
                                f"Documents ingested: {len(cm.registry)}\n"
                                f"Categories: {cats}\n")

    mirofish_url = (S.settings.get("mirofish_url") or "").strip()
    try:
        sim_id = S.simulator.start(
            decision_text=decision,
            personas=personas,
            depth=depth,
            company_context=company_context,
            company_id=company_id,
            client=S.dg.client,
            embed_model=S.dg.embed_model,
            mirofish_url=mirofish_url,
        )
        return {"sim_id": sim_id, "mode": "mirofish" if mirofish_url else "local"}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/simulation/{sim_id}")
def simulation_get(sim_id: str):
    sim = S.simulator.get(sim_id)
    if not sim: raise HTTPException(404, "simulation not found")
    return sim.to_dict()


@app.get("/api/simulation")
def simulation_list():
    return {"simulations": [
        {"id": s.id, "decision_text": s.decision_text[:80],
         "status": s.status, "progress": s.progress,
         "started_at": s.started_at, "consensus_score": (s.report or {}).get("consensus_score")}
        for s in sorted(S.simulator.list_all(), key=lambda x: x.started_at, reverse=True)
    ]}


@app.post("/api/simulation/{sim_id}/store")
def simulation_store_as_decision(sim_id: str):
    """Persist the simulation report as a decision node in DecisionMemory."""
    require_ready()
    sim = S.simulator.get(sim_id)
    if not sim:
        raise HTTPException(404, "simulation not found")
    if sim.status != "done":
        raise HTTPException(400, f"simulation not finished (status={sim.status})")
    r = sim.report or {}
    answer = (
        f"Consensus score: {r.get('consensus_score','?')}/100\n\n"
        f"Risks:\n" + "\n".join(f"- {x}" for x in r.get("risks", [])) + "\n\n"
        f"Opportunities:\n" + "\n".join(f"- {x}" for x in r.get("opportunities", [])) + "\n\n"
        f"Timeline:\n" + "\n".join(f"- {k}: {v}" for k, v in (r.get("timeline") or {}).items())
    )
    # decide where to persist — if simulation was company-scoped, store in that company
    target_mem = S.dg.memory
    if sim.company_id:
        cm = S.hub.get_company(sim.company_id)
        if cm and cm.company_dg:
            target_mem = cm.company_dg.memory
    did = target_mem.store(
        question=f"[SIMULATION] {sim.decision_text}",
        answer=answer,
        reasoning_summary=f"MiroFish-style simulation across {len(sim.personas)} personas, {sim.depth} rounds",
        communities_used=[],
        context_triples=[],
    )
    target_mem.save()
    return {"ok": True, "decision_id": did, "scope": sim.company_id or "personal"}

# LLM is server-managed in deployment mode. These endpoints are intentionally
@app.post("/api/workspace/clear")
async def clear_workspace(req: Request):
    """Wipe ALL graph + decision data in the current workspace's personal
    DG. Leaves the workspace folder structure intact but empties the contents
    (graph, decisions, communities, summaries, checkpoints, github cache).
    The user has to confirm in the UI — there's no undo."""
    require_ready()
    body = await req.json() if (await req.body()) else {}
    if (body.get("confirm") or "").strip().lower() != "yes":
        raise HTTPException(400, "must pass {confirm: 'yes'} to clear")
    ws = _CURRENT_WS.get()
    personal_dir = os.path.join(ws.root, "personal")
    removed: list[str] = []
    if os.path.isdir(personal_dir):
        for name in list(os.listdir(personal_dir)):
            p = os.path.join(personal_dir, name)
            try:
                if os.path.isfile(p):
                    os.unlink(p); removed.append(name)
                elif os.path.isdir(p):
                    import shutil as _sh
                    _sh.rmtree(p, ignore_errors=True); removed.append(name + "/")
            except Exception:
                pass
    # also wipe activities + github cache + skus if they exist (Day-3/Day-1 features)
    for sub in ("activities", "github_cache", "skus"):
        d = os.path.join(ws.root, sub)
        if os.path.isdir(d):
            import shutil as _sh
            _sh.rmtree(d, ignore_errors=True); removed.append(sub + "/")
    # force the workspace to rebuild its in-memory objects on next access
    try:
        ws.unload()
    except Exception:
        pass
    return {"cleared": True, "removed": removed,
            "workspace": ws.token,
            "note": "workspace is now empty — next page load will rebuild a fresh DG"}


# ── forecast (TimesFM / Holt-Winters / linear fallback chain) ─────────────────
@app.post("/api/forecast")
async def api_forecast(req: Request):
    """Time-series forecast for the Simulation Studio UI.
    Routes through decisiongraph.forecasting which tries TimesFM service first,
    then Holt-Winters, then linear fallback. Returns backend used."""
    require_ready()
    rate_limit("forecast", max_n=30, window_s=60)
    body = await req.json() if (await req.body()) else {}
    series = body.get("values") or body.get("series") or []
    horizon = int(body.get("horizon", 8))
    if not isinstance(series, list) or len(series) < 4:
        raise HTTPException(400, "values must be a list of at least 4 numbers")
    if horizon < 1 or horizon > 128:
        raise HTTPException(400, "horizon must be 1..128")
    try:
        series = [float(x) for x in series]
    except Exception:
        raise HTTPException(400, "values must all be numeric")
    from decisiongraph.forecasting import forecast as _fc, detect_backend
    try:
        r = _fc(series, horizon=horizon)
        r["backend_detected"] = detect_backend()
        return r
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


@app.get("/api/forecast/backend_info")
def api_forecast_backend():
    from decisiongraph.forecasting import detect_backend
    return {"backend": detect_backend()}


@app.post("/api/forecast/ask")
async def api_forecast_ask(req: Request):
    """Natural-language forecast: 'what will revenue be next 4 years?'
    Pipeline:
      1) LLM extracts {metric, horizon, unit} from the question
      2) Server pulls any numeric mentions of `metric` from decisions/sessions
      3) If <4 numeric pts found, LLM synthesises a plausible history series
         grounded in whatever graph context exists for the metric
      4) TimesFM forecasts the resulting series
      5) LLM writes a one-paragraph narrative answer with the numbers"""
    require_ready()
    global_llm_gate()
    rate_limit("forecast_ask", max_n=10, window_s=60)
    body = await req.json() if (await req.body()) else {}
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question required")

    import json as _json, re as _re
    client = S.dg.client
    model  = S.creds.get("model") or "gemini-3-flash"

    # ── step 1: extract metric + horizon ──────────────────────────────────
    extract_prompt = (
        "Extract structured info from this forecasting question. "
        "Respond ONLY in compact JSON, no prose.\n"
        'Schema: {"metric": str, "horizon": int, "unit": str}\n'
        '- "metric": short name of what to forecast (e.g. "revenue", "users", "ARR")\n'
        '- "horizon": how many future periods (default 8 if unclear)\n'
        '- "unit": one of "year","quarter","month","week","day"\n'
        f"Question: {question}"
    )
    try:
        r = client.messages.create(
            model=model, max_tokens=2000,
            messages=[{"role": "user", "content": extract_prompt}],
        )
        raw = "".join(getattr(b, "text", "") for b in r.content).strip()
        m = _re.search(r"\{.*\}", raw, _re.S)
        meta = _json.loads(m.group(0)) if m else {}
    except Exception as e:
        meta = {}
    metric  = (meta.get("metric") or "value").strip().lower()
    horizon = max(1, min(64, int(meta.get("horizon") or 8)))
    unit    = (meta.get("unit") or "period").lower()

    # ── step 2: scan decisions for numeric mentions of the metric ─────────
    rows = []
    try:
        rows = S.dg.memory.all_compiled().get("decisions", [])
    except Exception:
        pass
    # gather any text snippet that mentions the metric, ordered by timestamp
    rows = sorted(rows, key=lambda r: r.get("timestamp",""))
    metric_words = [w for w in _re.split(r"\W+", metric) if len(w) > 2]
    hits = []
    for row in rows:
        blob = " ".join(str(row.get(k,"")) for k in
                        ("question","answer","reasoning_summary","outcome"))
        if any(w in blob.lower() for w in metric_words) or not metric_words:
            hits.append({"ts": row.get("timestamp",""), "text": blob[:600]})
    # extract numbers from hits (currency, %, plain)
    series_from_graph: list[float] = []
    NUM_RE = _re.compile(r"(?<![\w])\$?\s?(\d{1,3}(?:[,.]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s?[%kKmMbB]?")
    for h in hits:
        for tok in NUM_RE.findall(h["text"]):
            try:
                v = float(tok.replace(",",""))
                if 0 < v < 1e12:
                    series_from_graph.append(v)
            except Exception:
                pass
    series_from_graph = series_from_graph[:32]  # cap

    source = "graph"
    series: list[float] = series_from_graph[:]

    # ── step 3: if not enough graph numbers, ask LLM to synthesise ─────────
    if len(series) < 4:
        source = "llm-grounded" if hits else "llm-inferred"
        ctx = "\n".join(f"- {h['ts'][:10]}: {h['text']}" for h in hits[:6]) or "(no prior context)"
        synth_prompt = (
            f"You are estimating a historical time series of {metric!r} "
            f"per {unit}. Use the prior decision-graph context below if any.\n"
            f"Return ONLY a JSON array of 12 numeric values (oldest → newest), "
            f"realistic for the domain, no commentary.\n\n"
            f"Context:\n{ctx}\n\nJSON array:"
        )
        try:
            r = client.messages.create(
                model=model, max_tokens=2500,
                messages=[{"role":"user","content":synth_prompt}],
            )
            raw = "".join(getattr(b, "text", "") for b in r.content).strip()
            m = _re.search(r"\[.*?\]", raw, _re.S)
            if m:
                arr = _json.loads(m.group(0))
                series = [float(x) for x in arr if isinstance(x,(int,float))][:24]
        except Exception:
            pass

    if len(series) < 4:
        raise HTTPException(422, "could not assemble a numeric series for that metric")

    # ── step 4: forecast ─────────────────────────────────────────────────
    from decisiongraph.forecasting import forecast as _fc, detect_backend
    fc = _fc(series, horizon=horizon)
    fc["backend_detected"] = detect_backend()

    # ── step 5: narrative answer ─────────────────────────────────────────
    last = series[-1]
    final = fc["point"][-1]
    pct = ((final - last) / last * 100.0) if last else 0.0
    nar_prompt = (
        f"In one short paragraph (<=80 words), answer this question using the "
        f"forecast numbers. Be concrete, cite first/last forecast values, "
        f"mention {unit} units, and note this is a TimesFM projection.\n"
        f"Question: {question}\n"
        f"Metric: {metric}\n"
        f"Recent history (last 6): {series[-6:]}\n"
        f"Forecast next {horizon} {unit}(s): {[round(v,2) for v in fc['point']]}\n"
        f"Change vs last observed: {pct:+.1f}%"
    )
    answer = ""
    try:
        r = client.messages.create(
            model=model, max_tokens=1500,
            messages=[{"role":"user","content":nar_prompt}],
        )
        answer = "".join(getattr(b, "text", "") for b in r.content).strip()
    except Exception:
        answer = (f"Projected {metric} over the next {horizon} {unit}(s): "
                  f"{round(final,2)} ({pct:+.1f}% vs last observed {round(last,2)}).")

    return {
        "question": question,
        "metric":   metric,
        "horizon":  horizon,
        "unit":     unit,
        "source":   source,   # 'graph' | 'llm-grounded' | 'llm-inferred'
        "history":  series,
        "forecast": fc,
        "answer":   answer,
    }


@app.get("/api/forecast/series")
def api_forecast_series():
    """Expose pre-built numeric series derived from the active workspace's
    decision graph + simulation history. The Simulation Studio uses these
    to forecast from real signal rather than hand-typed numbers."""
    require_ready()
    out: list[dict] = []

    # 1) Confidence over time — chronological confidence values of all decisions
    try:
        rows = S.dg.memory.all_compiled().get("decisions", [])
        rows = sorted(rows, key=lambda r: r.get("timestamp", ""))
        conf = [round(float(r.get("confidence", 0.0)), 3) for r in rows
                if r.get("confidence") is not None]
        if len(conf) >= 4:
            out.append({
                "id": "confidence_over_time",
                "label": f"Decision confidence over time ({len(conf)} pts)",
                "values": conf,
                "unit": "confidence (0-1)",
            })
    except Exception:
        pass

    # 2) Decisions per day — count grouped by ISO date
    try:
        from collections import Counter
        rows = S.dg.memory.all_compiled().get("decisions", [])
        days = Counter()
        for r in rows:
            t = (r.get("timestamp") or "")[:10]
            if t: days[t] += 1
        ordered = [days[k] for k in sorted(days.keys())]
        if len(ordered) >= 4:
            out.append({
                "id": "decisions_per_day",
                "label": f"Decisions logged per day ({len(ordered)} days)",
                "values": ordered,
                "unit": "decisions/day",
            })
    except Exception:
        pass

    # 3) Simulation consensus scores — chronological
    try:
        sims = S.simulator.list_all()
        sims = [s for s in sims if s.report and "consensus_score" in (s.report or {})]
        sims.sort(key=lambda s: s.started_at)
        scores = [int(s.report["consensus_score"]) for s in sims]
        if len(scores) >= 4:
            out.append({
                "id": "simulation_consensus",
                "label": f"Simulation consensus scores ({len(scores)} sims)",
                "values": scores,
                "unit": "consensus 0-100",
            })
    except Exception:
        pass

    # 4) Session activity — messages per session in chronological order
    try:
        sess = S.dm.list_sessions()
        sess = sorted(sess, key=lambda s: s.get("started_at",""))
        mcounts = [int(s.get("messages_count", 0)) for s in sess]
        if len(mcounts) >= 4:
            out.append({
                "id": "messages_per_session",
                "label": f"Messages per session ({len(mcounts)} sessions)",
                "values": mcounts,
                "unit": "messages",
            })
    except Exception:
        pass

    # 5) Cumulative graph growth — running total of decisions through time
    try:
        rows = S.dg.memory.all_compiled().get("decisions", [])
        rows = sorted(rows, key=lambda r: r.get("timestamp",""))
        if len(rows) >= 4:
            cum = list(range(1, len(rows)+1))
            out.append({
                "id": "graph_growth",
                "label": f"Cumulative decisions ({len(cum)} pts)",
                "values": cum,
                "unit": "decisions (cumulative)",
            })
    except Exception:
        pass

    # 6) AUTO-SEED — if the workspace is brand-new, hand back a synthetic
    # demo series so the UI immediately shows TimesFM working end-to-end
    # instead of an empty state. Marked `synthetic=True` so the UI can label it.
    if not out:
        import math, random, hashlib
        seed = int(hashlib.md5(getattr(S.ws,'token','demo').encode()).hexdigest()[:8], 16)
        rnd = random.Random(seed)
        base = [10 + i*0.6 + 2.5*math.sin(i/2.0) + rnd.uniform(-0.4, 0.4)
                for i in range(24)]
        out.append({
            "id": "demo_seed",
            "label": "Demo trend (auto-generated — log decisions to replace)",
            "values": [round(v, 2) for v in base],
            "unit": "demo units",
            "synthetic": True,
        })

    return {"series": out}


# inert so a visitor can't hijack or kill the shared connection.
@app.post("/api/connect")
async def api_connect(req: Request):
    # accept + ignore — keeps old clients from erroring; never changes server LLM
    return {"ok": True, "error": "", "managed": True}

@app.post("/api/disconnect")
def api_disconnect():
    raise HTTPException(status_code=403, detail="LLM connection is server-managed.")

# ── ingest ────────────────────────────────────────────────────────────────────
MAX_INGEST_FILES = 10
MAX_FILE_BYTES   = 5 * 1024 * 1024   # 5 MB/file

async def _guarded_ingest(files, ingest_fn):
    """Shared ingest path with abuse caps. Reads each file once, enforces
    count + size limits, runs the (LLM-backed) ingest, cleans up temps."""
    rate_limit("ingest", max_n=20, window_s=3600)   # 20 ingests/hour/workspace
    if len(files) > MAX_INGEST_FILES:
        raise HTTPException(413, f"Too many files (max {MAX_INGEST_FILES} per request).")
    results = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_FILE_BYTES:
            results.append({"file": f.filename, "ok": False,
                            "error": f"file exceeds {MAX_FILE_BYTES//(1024*1024)}MB limit"})
            continue
        global_llm_gate()
        suf = Path(f.filename).suffix or ".txt"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suf)
        tmp.write(data); tmp.close()
        try:
            ingest_fn(tmp.name)
            results.append({"file": f.filename, "ok": True})
        except Exception as e:
            results.append({"file": f.filename, "ok": False, "error": str(e)})
        finally:
            try: os.unlink(tmp.name)
            except Exception: pass
    return results

@app.post("/api/ingest/personal")
async def ingest_personal(files: list[UploadFile] = File(...)):
    require_ready()
    results = await _guarded_ingest(files, S.dg.ingest)
    return {"results": results, "stats": S.dg.stats()}

@app.post("/api/ingest/company/{company_id}")
async def ingest_company(company_id: str, files: list[UploadFile] = File(...)):
    require_ready()
    cm = S.hub.get_company(company_id)
    if not cm: raise HTTPException(404, f"Company '{company_id}' not found")
    results = await _guarded_ingest(files, cm.ingest)
    return {"results": results, "stats": cm.stats()}

# ── non-file sources: URL / YouTube / media → graph ───────────────────────────
def _resolve_ingest_target(company_id):
    """Returns (ingest_fn, stats_fn). company_id optional."""
    if company_id:
        cm = S.hub.get_company(company_id)
        if not cm: raise HTTPException(404, f"Company '{company_id}' not found")
        return cm.ingest, cm.stats
    return S.dg.ingest, S.dg.stats

# Web/media ingest is ASYNC via the durable queue — triple-extraction on a
# real article takes >100s and Cloudflare quick-tunnels hard-cut at ~100s
# (524). Enqueue → return job_id → frontend polls /api/jobs/{id}.
@app.post("/api/ingest/url")
async def ingest_url(req: Request):
    require_ready()
    rate_limit("ingest_web", max_n=15, window_s=3600)
    body = await req.json()
    url = (body.get("url") or "").strip()
    if not url: raise HTTPException(400, "url required")
    ws = _CURRENT_WS.get()
    jid = JOBS.enqueue("ingest", {"kind": "url", "url": url,
                                  "workspace": ws.token,
                                  "company_id": body.get("company_id")},
                       workspace=ws.token)
    return {"job_id": jid, "status": "queued", "source": "url"}

@app.post("/api/ingest/youtube")
async def ingest_youtube(req: Request):
    require_ready()
    rate_limit("ingest_web", max_n=15, window_s=3600)
    body = await req.json()
    url = (body.get("url") or "").strip()
    if not url: raise HTTPException(400, "url required")
    ws = _CURRENT_WS.get()
    jid = JOBS.enqueue("ingest", {"kind": "youtube", "url": url,
                                  "workspace": ws.token,
                                  "company_id": body.get("company_id")},
                       workspace=ws.token)
    return {"job_id": jid, "status": "queued", "source": "youtube"}

@app.post("/api/ingest/media")
async def ingest_media(file: UploadFile = File(...), company_id: str = Form(None)):
    require_ready()
    rate_limit("ingest_media", max_n=5, window_s=3600)
    key = S.settings.get("gemini_api_key", "")
    if not key:
        raise HTTPException(503, "Media transcription not configured on server.")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "media exceeds 25MB limit")
    ws = _CURRENT_WS.get()
    suf = Path(file.filename).suffix or ".mp3"
    mt = tempfile.NamedTemporaryFile(delete=False, suffix=suf)
    mt.write(data); mt.close()
    jid = JOBS.enqueue("ingest", {"kind": "media", "media_path": mt.name,
                                  "gemini_key": key, "workspace": ws.token,
                                  "company_id": company_id},
                       workspace=ws.token)
    return {"job_id": jid, "status": "queued", "source": "media"}

# ── github repo ingest (v0 — sync; later: async via job queue) ───────────────
@app.post("/api/ingest/github")
async def ingest_github(req: Request):
    """Drop a github.com URL → DG ingests README/docs/ADRs/manifests/commits
    AND a per-file LLM summary of source code. Whole repo becomes queryable."""
    require_ready()
    global_llm_gate()
    rate_limit("ingest_github", max_n=5, window_s=3600)
    body = await req.json() if (await req.body()) else {}
    repo_url = (body.get("repo_url") or "").strip()
    branch   = (body.get("branch") or "").strip() or None
    include_code         = bool(body.get("include_code",         True))
    use_ast              = bool(body.get("use_ast",              True))
    include_call_edges   = bool(body.get("include_call_edges",   True))
    include_hierarchical = bool(body.get("include_hierarchical", True))
    include_prs          = bool(body.get("include_prs",          True))
    incremental          = bool(body.get("incremental",          True))
    if not repo_url:
        raise HTTPException(400, "repo_url required")

    from decisiongraph.codebase import _parse_github_url
    if not _parse_github_url(repo_url):
        raise HTTPException(400, "must be a public github.com URL")

    ws = _CURRENT_WS.get()
    # Async via job queue — the ingest can take 1–3 min for big repos and
    # would exceed any reverse-proxy timeout if run synchronously.
    # The browser polls /api/jobs/<id> for progress + final stats.
    jid = JOBS.enqueue("ingest_github", {
        "workspace":            ws.token,
        "repo_url":             repo_url,
        "branch":               branch or "",
        "include_code":         include_code,
        "use_ast":              use_ast,
        "include_call_edges":   include_call_edges,
        "include_hierarchical": include_hierarchical,
        "include_prs":          include_prs,
        "incremental":          incremental,
    }, workspace=ws.token)
    return {"job_id": jid, "status": "queued", "source": "github",
            "repo_url": repo_url}


# ── companies ─────────────────────────────────────────────────────────────────
@app.get("/api/companies")
def list_companies():
    require_ready()
    out = []
    for c in S.hub.list_companies():
        cm = S.hub.get_company(c["id"])
        out.append({
            "id": c["id"],
            "name": c["name"],
            "documents": len(cm.registry) if cm else 0,
            "categories": cm.stats().get("categories", {}) if cm else {},
        })
    return {"companies": out}

@app.post("/api/companies")
async def create_company(req: Request):
    require_ready()
    body = await req.json()
    cid = body.get("id","").strip().lower()
    nm  = body.get("name","").strip()
    if not cid or not nm: raise HTTPException(400, "id and name required")
    S.hub.add_company(cid, nm)
    return {"ok": True, "id": cid, "name": nm}

@app.get("/api/companies/{company_id}/documents")
def company_docs(company_id: str):
    require_ready()
    cm = S.hub.get_company(company_id)
    if not cm: raise HTTPException(404)
    return {"documents": cm.list_documents()}

@app.get("/api/repo/list")
async def list_ingested_repos(req: Request):
    """List all GitHub repos this workspace has ingested, de-duplicated by
    canonical owner/repo form. If we see both `owner/X` and bare `X`, they
    get merged into a single `owner/X` entry."""
    require_ready()
    import re as _re
    ws = _CURRENT_WS.get()
    rows = ws.dg.memory.all_decisions()

    # collect raw mentions
    raw_counts: dict[str, int] = {}
    for r in rows:
        q = r.get("question") or ""
        rs = r.get("reasoning_summary") or ""
        for m in _re.findall(r"repo=([\w\-./]+)", rs):
            raw_counts[m] = raw_counts.get(m, 0) + 1
        for m in _re.findall(r"\[repo:([^\]]+)\]", q + " " + rs):
            raw_counts[m] = raw_counts.get(m, 0) + 1
        m = _re.search(r"of the (\w[\w\-]*) codebase", q)
        if m:
            raw_counts[m.group(1)] = raw_counts.get(m.group(1), 0) + 1

    # canonicalise: prefer owner/repo form. If "owner/X" exists, fold any
    # bare "X" mentions into it. Otherwise keep bare form.
    canonical: dict[str, int] = {}
    has_slash = [k for k in raw_counts if "/" in k]
    short_to_full = {k.split("/")[-1]: k for k in has_slash}
    for k, v in raw_counts.items():
        if "/" in k:
            canonical[k] = canonical.get(k, 0) + v
        elif k in short_to_full:
            full = short_to_full[k]
            canonical[full] = canonical.get(full, 0) + v
        else:
            canonical[k] = canonical.get(k, 0) + v

    repos = [{"name": k, "items": v}
                for k, v in sorted(canonical.items(), key=lambda x: -x[1])]
    return {"repos": repos}


# ── repo-aware ask: pulls EVERYTHING tagged with a given repo and answers ─────
@app.post("/api/repo/ask")
async def repo_ask(req: Request):
    """When the user asks about an ingested repo specifically, we shouldn't
    do top-K semantic retrieval (it misses the README, misses the overview,
    misses most files). Instead pull ALL DG decisions that came from that
    repo and feed them as one consolidated context to the LLM.

    Body: {question: str, repo: str?}.
    If repo is omitted, we try to infer it from the question (matches any
    repo name we've seen in the decision log)."""
    require_ready()
    global_llm_gate()
    rate_limit("repo_ask", max_n=15, window_s=60)
    body = await req.json() if (await req.body()) else {}
    question = (body.get("question") or "").strip()
    repo_hint = (body.get("repo") or "").strip()
    # graph_focus=True → use semantic seed + call-graph traversal to focus the
    # context on chunks RELEVANT to the question + their callers/callees,
    # instead of dumping every chunk. Default off (dump all) since most repos
    # fit easily. Turn on for very large repos or focused questions.
    graph_focus = bool(body.get("graph_focus", False))
    if not question:
        raise HTTPException(400, "question required")

    ws = _CURRENT_WS.get()
    rows = ws.dg.memory.all_decisions()

    # ── step 1: find all repo names we've ingested
    import re as _re
    repos_seen: dict[str, int] = {}
    pat = _re.compile(r"\[repo:([^\]]+)\]|reasoning_summary.*?repo=([\w\-./]+)")
    for r in rows:
        q = r.get("question") or ""
        rs = r.get("reasoning_summary") or ""
        m1 = _re.search(r"\[repo:([^\]]+)\]", q + " " + rs)
        m2 = _re.search(r"repo=([\w\-./]+)", rs)
        m3 = _re.search(r"of the (\w[\w\-]*) codebase", q)
        m4 = _re.search(r"of (\w[\w\-]*) repository", q)
        for m in (m1, m2, m3, m4):
            if m:
                rn = m.group(1).strip()
                repos_seen[rn] = repos_seen.get(rn, 0) + 1
    if not repos_seen:
        raise HTTPException(404, "no ingested repos found in this workspace")

    # ── step 2: pick the target repo
    chosen = None
    if repo_hint:
        # exact or substring match
        for r in repos_seen:
            if r.lower() == repo_hint.lower() or repo_hint.lower() in r.lower():
                chosen = r; break
    if not chosen:
        # try to find repo name in the question
        ql = question.lower()
        for r in sorted(repos_seen, key=lambda x: -len(x)):
            if r.lower() in ql:
                chosen = r; break
    if not chosen:
        # default: most-mentioned repo
        chosen = max(repos_seen, key=lambda r: repos_seen[r])

    # ── step 3: gather EVERYTHING tagged with this repo
    bucket = {"docs": [], "manifest": [], "commits": [], "prs": [],
                "folders": [], "files": [], "chunks": [], "overview": [], "other": []}
    for r in rows:
        q = (r.get("question") or "")
        rs = (r.get("reasoning_summary") or "")
        blob = q + " " + rs
        # is this row associated with the chosen repo?
        if (f"[repo:{chosen}]" not in blob and
            f"of the {chosen} codebase" not in q and
            f"of {chosen}/" not in rs and
            f"repo={chosen}" not in rs and
            f"of the {chosen} repository" not in q and
            f"repo=" + chosen.split("/")[-1] not in rs):
            # also try by short repo name (after slash)
            short = chosen.split("/")[-1]
            if (f"[repo:{short}]" not in blob and
                f"of the {short} codebase" not in q and
                f"repo={short}" not in rs):
                continue
        # bucket it
        ans = r.get("answer") or ""
        ts = r.get("timestamp") or ""
        item = {"question": q, "answer": ans, "ts": ts}
        ql = q.lower()
        if "what is the" in ql and ("project" in ql or "codebase" in ql or "repository" in ql):
            bucket["overview"].append(item)
        elif "kind=doc" in rs or "doc · " in q.lower():
            bucket["docs"].append(item)
        elif "kind=manifest" in rs or "manifest" in q.lower():
            bucket["manifest"].append(item)
        elif "PR #" in q or "kind=pr" in rs:
            bucket["prs"].append(item)
        elif "kind=commit" in rs or "commit · " in q.lower():
            bucket["commits"].append(item)
        elif "what is in the" in ql and "folder" in ql:
            bucket["folders"].append(item)
        elif "what does the file" in ql:
            bucket["files"].append(item)
        elif "what does the" in ql:
            bucket["chunks"].append(item)
        else:
            bucket["other"].append(item)

    counts = {k: len(v) for k, v in bucket.items() if v}
    if sum(counts.values()) == 0:
        raise HTTPException(404, f"no decisions found for repo '{chosen}'")

    # ── optional: graph-augmented focused retrieval ────────────────────────
    # Focus mode NARROWS the function-chunk noise, but ALWAYS keeps the
    # high-level prose (README, repo overview, manifests, folder rollups,
    # commits). Those are what answer "what is X" questions.
    focus_stats: dict | None = None
    if graph_focus and bucket["chunks"]:
        try:
            import numpy as _np
            embed = ws.dg.embed_model
            q_vec = embed.encode([question], convert_to_numpy=True)[0]
            texts = [c["question"] + " " + c["answer"][:300] for c in bucket["chunks"]]
            t_vecs = embed.encode(texts, convert_to_numpy=True)
            sims = t_vecs @ q_vec / (_np.linalg.norm(t_vecs, axis=1) *
                                       _np.linalg.norm(q_vec) + 1e-9)
            order = _np.argsort(-sims)
            seed_n = min(20, len(bucket["chunks"]))
            seeds = [bucket["chunks"][i] for i in order[:seed_n]]
            seed_names = set()
            import re as _re_focus
            for s in seeds:
                m = _re_focus.search(r"What does the (\S+)", s["question"])
                if m: seed_names.add(m.group(1))

            neighbour_names = set()
            if hasattr(ws.dg, "G") and ws.dg.G is not None:
                G = ws.dg.G
                for n in seed_names:
                    if G.has_node(n):
                        for succ in G.successors(n): neighbour_names.add(succ)
                        for pred in G.predecessors(n): neighbour_names.add(pred)
            extras = []
            seed_keys = {(c["question"], c["answer"]) for c in seeds}
            for c in bucket["chunks"]:
                key = (c["question"], c["answer"])
                if key in seed_keys: continue
                m = _re_focus.search(r"What does the (\S+)", c["question"])
                if m and m.group(1) in neighbour_names:
                    extras.append(c)

            focused = seeds + extras
            focus_stats = {
                "seeds":       len(seeds),
                "from_graph":  len(extras),
                "dropped":     len(bucket["chunks"]) - len(focused),
                "kept":        len(focused),
            }
            # NARROW only the chunks. README / overview / folders / commits
            # stay full so "what is X" type questions can answer.
            bucket["chunks"] = focused
            counts["chunks"] = len(focused)
        except Exception as e:
            focus_stats = {"error": str(e)}

    # ── step 4: build a HIERARCHICAL context block —
    # repo overview → README/docs → folders (each with its files+functions nested)
    # → commits → PRs. This mirrors the actual structure of the codebase, so the
    # LLM "sees" function→file→folder→repo relationships, not a flat dump.
    # Gemini Flash has 1M-token context window — we use up to ~250K chars
    # which is well within limits, so no aggressive capping needed.

    import os as _os, re as _re_local

    # Re-parse chunks/files to extract their (folder, file, name) for nesting
    structured_chunks = []   # [{folder, file, name, summary}]
    for it in bucket["chunks"]:
        q = it["question"]
        # match "What does the NAME kind do in the REPO codebase? (file: PATH, ...)"
        m = _re_local.search(r"What does the (\S+?) .+? in the .+? codebase\? \(file: ([^,)]+)", q)
        if m:
            name, path = m.group(1), m.group(2).strip()
            folder = _os.path.dirname(path) or "."
            structured_chunks.append({
                "folder": folder, "file": path, "name": name,
                "summary": it["answer"][:500]})
    for it in bucket["files"]:
        q = it["question"]
        m = _re_local.search(r"What does the file (\S+) do", q)
        if m:
            path = m.group(1).rstrip("?")
            folder = _os.path.dirname(path) or "."
            structured_chunks.append({
                "folder": folder, "file": path, "name": "(whole file)",
                "summary": it["answer"][:500]})

    # group by folder
    by_folder: dict[str, list[dict]] = {}
    for c in structured_chunks:
        by_folder.setdefault(c["folder"], []).append(c)

    parts = [
        f"You are answering a question about the GitHub repository `{chosen}`.",
        f"The DecisionGraph below contains the COMPLETE structured ingest of that",
        f"repo: README, manifest, commits, every function/class summary, every",
        f"folder roll-up, and the repo-level overview. Use this context only.",
        f"Do not invent details. If the question can't be answered from this",
        f"context, say so explicitly.",
        ""]

    # 1. Repo overview (most important — answers "what is X")
    if bucket["overview"]:
        parts.append("=== REPO OVERVIEW ===")
        for it in bucket["overview"]:
            parts.append(it["answer"][:2000])
        parts.append("")

    # 2. README + docs (rich prose, important for "what is" questions)
    if bucket["docs"]:
        parts.append(f"=== README / DOCS ({len(bucket['docs'])}) ===")
        for it in bucket["docs"]:
            parts.append(f"[{it['question'][:120]}]")
            parts.append(it["answer"][:3000])
            parts.append("")

    # 3. Manifest (deps + stack — answers "what tech does this use")
    if bucket["manifest"]:
        parts.append(f"=== STACK / MANIFEST ({len(bucket['manifest'])}) ===")
        for it in bucket["manifest"]:
            parts.append(it["answer"][:1500])
        parts.append("")

    # 4. HIERARCHICAL STRUCTURE — folder → files → functions
    # This is where function-to-file-to-folder linking lives.
    if by_folder or bucket["folders"]:
        parts.append("=== CODEBASE STRUCTURE (folder → files → functions) ===")
        # collect folder summaries by path so we can attach them
        folder_summary_by_path: dict[str, str] = {}
        for it in bucket["folders"]:
            m = _re_local.search(r"in the (\S+) folder", it["question"])
            if m:
                folder_summary_by_path[m.group(1)] = it["answer"][:600]
        # render each folder with its rollup + nested files/chunks
        all_folders = sorted(set(list(by_folder.keys()) + list(folder_summary_by_path.keys())))
        for folder in all_folders:
            parts.append(f"")
            parts.append(f"📁 {folder}/")
            if folder in folder_summary_by_path:
                parts.append(f"   — role: {folder_summary_by_path[folder]}")
            # files in this folder
            files_in_folder: dict[str, list[dict]] = {}
            for c in by_folder.get(folder, []):
                files_in_folder.setdefault(c["file"], []).append(c)
            for fpath, fns in files_in_folder.items():
                parts.append(f"   📄 {fpath}")
                for fn in fns:
                    if fn["name"] == "(whole file)":
                        parts.append(f"      → {fn['summary']}")
                    else:
                        parts.append(f"      • {fn['name']}: {fn['summary']}")
        parts.append("")

    # 5. Recent commits
    if bucket["commits"]:
        parts.append(f"=== RECENT COMMITS ({len(bucket['commits'])}) ===")
        for it in bucket["commits"][:25]:
            parts.append(f"• {it['question'][:200]}")
            if it["answer"]:
                parts.append(f"  {it['answer'][:300]}")
        parts.append("")

    # 6. PRs
    if bucket["prs"]:
        parts.append(f"=== RECENT PULL REQUESTS ({len(bucket['prs'])}) ===")
        for it in bucket["prs"][:20]:
            parts.append(f"• {it['question'][:200]}")
            if it["answer"]:
                parts.append(f"  {it['answer'][:400]}")
        parts.append("")

    # 7. Call-graph edges — surface function relationships from the knowledge graph
    # Match the chosen repo against the edge's `repo` attribute in either form:
    #   • full owner/repo (e.g. "ShaneBraiden/V")
    #   • bare repo name (e.g. "V")
    # Also accept partial matches against the src/dst file paths so we still
    # surface edges if the `repo` attr was set differently.
    call_edges_section = []
    if hasattr(ws.dg, "G") and ws.dg.G is not None:
        try:
            chosen_short = chosen.split("/")[-1].lower()
            chosen_full = chosen.lower()
            for u, v, data in ws.dg.G.edges(data=True):
                if not isinstance(data, dict): continue
                if data.get("label") != "calls": continue
                rep = (data.get("repo", "") or "").lower()
                src_path = (data.get("src_path", "") or "").lower()
                dst_path = (data.get("dst_path", "") or "").lower()
                # match any of: edge.repo attribute, src/dst path, src/dst file in same repo
                matches = (
                    chosen_full in rep or chosen_short in rep or
                    chosen_full in src_path or chosen_short in src_path or
                    chosen_full in dst_path or chosen_short in dst_path)
                if not matches:
                    continue
                # include the src file path inline so the LLM can ground it
                line = f"  • {u}  →  {v}"
                if data.get("src_path"):
                    line += f"   (in {data['src_path']})"
                call_edges_section.append(line)
        except Exception:
            pass
    if call_edges_section:
        parts.append(f"=== FUNCTION CALL GRAPH ({len(call_edges_section)} edges, who calls whom) ===")
        # cap the edges section to keep total context reasonable — 200 is plenty
        for e in call_edges_section[:200]:
            parts.append(e)
        if len(call_edges_section) > 200:
            parts.append(f"  …and {len(call_edges_section) - 200} more edges")
        parts.append("")

    # 8. Anything we couldn't bucket
    if bucket["other"]:
        parts.append(f"=== OTHER ({len(bucket['other'])}) ===")
        for it in bucket["other"][:15]:
            parts.append(f"• {it['question'][:200]}: {it['answer'][:300]}")
        parts.append("")

    parts.append("=== USER QUESTION ===")
    parts.append(question)
    parts.append("")
    parts.append("Answer in plain English. Reference specific files, folders, "
                  "functions, or call relationships from the context above. If "
                  "you cite a path, use the actual path shown. Be concrete.")
    big_context = "\n".join(parts)

    # Safety: if context is somehow gigantic (>800k chars), trim chunks first.
    if len(big_context) > 800_000:
        big_context = big_context[:800_000] + "\n…[context truncated to fit LLM window]"

    # ── step 5: ask the LLM
    client = ws.dg.client
    from decisiongraph import config as _cfg
    model = _cfg.LLM_MODEL or "gemini-3-flash"
    try:
        r = client.messages.create(
            model=model, max_tokens=3000,
            messages=[{"role": "user", "content": big_context}],
        )
        answer = "".join(getattr(b, "text", "") for b in r.content).strip()
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"LLM call failed: {e}")

    return {
        "repo":              chosen,
        "question":          question,
        "answer":            answer,
        "context_stats":     counts,
        "total_items_used":  sum(counts.values()),
        "repos_available":   sorted(repos_seen.keys()),
        "context_chars":     len(big_context),
        "call_edges_used":   len(call_edges_section),
        "graph_focus":       focus_stats,
    }


# ── query ─────────────────────────────────────────────────────────────────────
@app.post("/api/query")
async def api_query(req: Request):
    require_ready()
    rate_limit("query", max_n=30, window_s=60)    # 30 queries/min/workspace
    global_llm_gate()
    body = await req.json()
    q = (body.get("question") or "").strip()
    mode = body.get("mode","deep")
    company_id = body.get("company_id") or None
    if not q: raise HTTPException(400, "question required")

    # gbrain #8 — intent-routed retrieval. mode="auto" → classify → pick mode.
    routed_intent = None
    if mode == "auto":
        from decisiongraph.query import classify_intent, intent_to_mode
        routed_intent = classify_intent(q)
        mode = intent_to_mode(routed_intent)

    target = S.dg
    cmeta = None
    company_graphs = None     # multi-graph payload for dual-DG companies
    if company_id:
        cm = S.hub.get_company(company_id)
        if cm and (cm.company_dg.G is not None or cm.knowledge_dg.G is not None):
            target = cm.company_dg
            cats = {}
            for d in cm.registry.values():
                cats[d["category"]] = cats.get(d["category"], 0) + 1
            cmeta = {"name": cm.company_name, "documents_ingested": len(cm.registry), "categories": cats}
            # Task 5 — inject cross-company wisdom
            try:
                w = S.hub.get_wisdom(q, company_id=company_id, embed_model=cm.company_dg.embed_model)
                if w: cmeta["wisdom"] = w
            except Exception as e:
                print(f"  Wisdom lookup failed: {e}")
            company_graphs = cm._graphs_payload()

    # capture beam-hits + past matches
    beam_hits = []
    past = []
    try:
        import decisiongraph.query as qmod
        ob = qmod.beam_query
        omq = target.memory.query
        def pb(qq, G, cs, ci, ce, em, k=None):
            r = ob(qq, G, cs, ci, ce, em, k); tr, mt = r
            for cid in mt:
                beam_hits.append({"id": cid, "summary": cs.get(cid, {}).get("summary","")})
            return r
        def pmq(qq, em, top_k=3):
            r = omq(qq, em, top_k); past.extend(r); return r
        qmod.beam_query = pb; target.memory.query = pmq

        if target.G is None and mode != "normal":
            mode = "normal"

        # Pass uncertainty gate settings into the agent
        ug = {
            "uncertainty_enabled":  bool(S.settings.get("uncertainty_gate_enabled", False)),
            "uncertainty_threshold": float(S.settings.get("uncertainty_threshold", 0.3)),
        }
        if mode == "normal":
            from decisiongraph.agent import normal_mode
            ans = normal_mode(q, target.client, target.memory, target.embed_model)
        elif mode == "session":
            from decisiongraph.agent import session_mode
            if company_graphs:
                ans = session_mode(q, target.client, embed_model=target.embed_model,
                                   memory=target.memory, company_metadata=cmeta,
                                   graphs=company_graphs, **ug)
            else:
                ans = session_mode(q, target.client, target.G, target.summaries,
                                   target.community_ids, target.community_embeddings,
                                   target.embed_model, target.memory, cmeta, **ug)
        else:
            from decisiongraph.agent import react_agent
            if company_graphs:
                ans = react_agent(q, target.client, embed_model=target.embed_model,
                                  memory=target.memory, graphs=company_graphs, **ug)
            else:
                ans = react_agent(q, target.client, target.G, target.summaries,
                                  target.community_ids, target.community_embeddings,
                                  target.embed_model, target.memory, **ug)
        qmod.beam_query = ob; target.memory.query = omq

        # auto-broadcast the newly-created decision (last one in the list)
        try:
            decs = target.get_decisions() if hasattr(target, "get_decisions") else S.dg.get_decisions()
            if decs: _auto_broadcast_decision_async(decs[-1])
        except Exception as e:
            print(f"[auto-broadcast/decision] skipped: {e}")

        # gbrain #2 — hybrid retrieval: fuse the vector beam hits with a
        # zero-LLM keyword pass via RRF, surfaced alongside the answer.
        hybrid_context = None
        if body.get("hybrid"):
            try:
                from decisiongraph.query import keyword_search, rrf_fuse
                G = getattr(target, "G", None)
                kw = keyword_search(q, G)
                vec = [f"#{h['id']} {h.get('summary','')}" for h in beam_hits]
                hybrid_context = rrf_fuse(vec, kw, limit=20)
            except Exception as e:
                print(f"[hybrid] skipped: {e}")

        # gbrain #9 — capture this query for retrieval-regression replay
        try:
            from decisiongraph import evals_capture
            _ws = _CURRENT_WS.get()
            evals_capture.capture(
                _ws.root if _ws else str(ROOT),
                question=q, mode=mode, intent=routed_intent,
                communities=len(beam_hits))
        except Exception: pass

        return {"answer": ans, "mode": mode, "intent": routed_intent,
                "communities": beam_hits, "past_decisions": past,
                "hybrid_context": hybrid_context}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))

# ── decisions ─────────────────────────────────────────────────────────────────
@app.get("/api/decisions")
def list_decisions(active_only: bool = False):
    require_ready()
    decs = S.dg.get_decisions()
    if active_only:
        decs = [d for d in decs if d.get("is_active", True)]
    return {"decisions": decs}

@app.post("/api/decisions/{decision_id}/outcome")
async def record_outcome(decision_id: str, req: Request):
    """Task 4 — record real-world outcome of a decision."""
    require_ready()
    body = await req.json()
    outcome = body.get("outcome", "unknown")
    if outcome not in {"unknown", "success", "failure", "partial"}:
        raise HTTPException(400, "outcome must be one of: unknown|success|failure|partial")
    ok = S.dg.memory.update_outcome(
        decision_id, outcome,
        notes=body.get("notes", ""),
        impact=float(body.get("impact", 0.0)),
    )
    if not ok: raise HTTPException(404, "decision not found")
    S.dg.memory.save()
    return {"ok": True}

@app.get("/api/decisions/{decision_id}/chain")
def decision_chain(decision_id: str):
    """Task 3 — return the lineage of a decision (caused_by/depends_on/led_to/related_to)."""
    require_ready()
    return S.dg.memory.get_decision_chain(decision_id)

@app.get("/api/decisions/patterns")
def outcome_patterns():
    """Task 4 — per-community success/failure rates across decisions."""
    require_ready()
    return {"patterns": S.dg.memory.get_outcome_patterns()}

# ── gbrain #3: recall hot path (fast, NO LLM) ─────────────────────────────────
@app.get("/api/recall")
def recall(entity: str, limit: int = 8):
    """Instant entity recall: active, high-confidence decisions mentioning
    `entity`, newest first + that node's graph neighbourhood. No LLM, no beam."""
    require_ready()
    rate_limit("recall", max_n=120, window_s=60)
    e = (entity or "").strip().lower()
    if not e:
        raise HTTPException(400, "entity required")
    hits = []
    for d in S.dg.memory.get_active_decisions():
        blob = f"{d.get('question','')} {d.get('answer','')}".lower()
        if e in blob:
            hits.append(d)
    hits.sort(key=lambda d: (d.get("confidence", 0), d.get("timestamp", "")), reverse=True)
    # graph neighbourhood
    neighbours = []
    G = S.dg.G
    if G is not None:
        for n in G.nodes():
            if e in str(n).lower():
                for u, v, data in list(G.edges(n, data=True))[:10]:
                    neighbours.append(f"{u} --[{data.get('relation','')}]--> {v}")
                break
    return {"entity": entity, "facts": hits[:limit],
            "graph_neighbourhood": neighbours[:10]}

# ── gbrain #1: Compiled Truth + Timeline ──────────────────────────────────────
@app.get("/api/compiled/{community_id}")
def get_compiled(community_id: str):
    require_ready()
    try: cid = int(community_id)
    except Exception: cid = community_id
    return S.dg.memory.get_compiled(cid)

@app.post("/api/compiled/{community_id}/recompile")
def recompile(community_id: str):
    require_ready()
    rate_limit("compile", max_n=10, window_s=300)
    global_llm_gate()
    try: cid = int(community_id)
    except Exception: cid = community_id
    summ = (S.dg.summaries or {}).get(cid, {}).get("summary", "") if S.dg.summaries else ""
    text = S.dg.memory.compile_topic(cid, S.dg.client, topic_summary=summ)
    S.dg.memory.save()
    return {"community": cid, "compiled_truth": text}

@app.get("/api/compiled")
def list_compiled():
    require_ready()
    return {"compiled": S.dg.memory.all_compiled()}

# ── gbrain #9: eval capture + replay ──────────────────────────────────────────
@app.get("/api/evals")
def evals_list():
    require_ready()
    from decisiongraph import evals_capture
    ws = _CURRENT_WS.get()
    return {"captured": evals_capture.load(ws.root if ws else str(ROOT), limit=100)}

@app.post("/api/evals/replay")
def evals_replay():
    """Re-run captured queries through retrieval only (no LLM) — regression signal."""
    require_ready()
    rate_limit("evalreplay", max_n=5, window_s=300)
    from decisiongraph import evals_capture
    ws = _CURRENT_WS.get()
    return evals_capture.replay(S.dg, ws.root if ws else str(ROOT))

# ── gbrain #4: Dream Cycle (durable job) ──────────────────────────────────────
@app.post("/api/dream/run")
def dream_run():
    """Enqueue a Dream Cycle for THIS workspace. Restart-safe (SQLite queue)."""
    require_ready()
    rate_limit("dream", max_n=3, window_s=3600)
    ws = _CURRENT_WS.get()
    jid = JOBS.enqueue("dream", {"workspace": ws.token}, workspace=ws.token)
    return {"job_id": jid, "status": "queued"}

@app.get("/api/jobs")
def list_jobs():
    require_ready()
    ws = _CURRENT_WS.get()
    return {"jobs": JOBS.list(workspace=ws.token if ws else None, limit=30)}

@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    require_ready()
    j = JOBS.get(job_id)
    if not j: raise HTTPException(404, "job not found")
    # scope: a visitor can only see their own jobs
    ws = _CURRENT_WS.get()
    if ws and j.get("workspace") and j["workspace"] != ws.token:
        raise HTTPException(403, "not your job")
    return j

# ── gbrain #7: markdown export of THIS workspace ──────────────────────────────
@app.get("/api/workspace/export")
def export_markdown():
    """Human-readable brain export — Compiled Truth + Timeline per topic,
    plus decisions and sessions. Returned as a single markdown document."""
    require_ready()
    rate_limit("export", max_n=10, window_s=300)
    ws = _CURRENT_WS.get()
    mem = S.dg.memory
    lines = [f"# DecisionGraph Brain Export",
             f"_workspace `{ws.token[:8] if ws else '?'}` · {time.strftime('%Y-%m-%d %H:%M')}_\n"]
    # compiled truths + timelines
    compiled = mem.all_compiled()
    if compiled:
        lines.append("## Compiled Truths\n")
        for cid, c in compiled.items():
            lines.append(f"### Topic {cid}\n")
            lines.append(f"**Current understanding:** {c.get('text','')}\n")
            lines.append(f"_compiled {c.get('compiled_at','')} · {c.get('n',0)} evidence_\n")
            tl = mem.get_timeline(int(cid) if str(cid).isdigit() else cid)
            if tl:
                lines.append("**Timeline (immutable evidence):**\n")
                for r in tl:
                    lines.append(f"- `{r['timestamp'][:16]}` [{r['outcome']}] "
                                 f"{r['question']} → {r['answer'][:160]}")
                lines.append("")
    # all decisions
    decs = S.dg.get_decisions()
    lines.append(f"\n## Decisions ({len(decs)})\n")
    for d in decs:
        lines.append(f"### {d.get('question','')}\n")
        lines.append(f"{d.get('answer','')}\n")
        lines.append(f"_id={d.get('id')} · outcome={d.get('outcome')} · "
                     f"confidence={d.get('confidence')} · {d.get('timestamp','')[:16]}_\n")
    # sessions
    try:
        sess = S.dm.list_sessions()
        if sess:
            lines.append(f"\n## Discussion Sessions ({len(sess)})\n")
            for s in sess:
                lines.append(f"### {s.get('title','(untitled)')}\n")
                lines.append(f"{s.get('summary','')}\n")
    except Exception:
        pass
    md = "\n".join(lines)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(md, headers={
        "Content-Disposition": 'attachment; filename="brain_export.md"'})

@app.post("/api/decisions/{old_id}/supersede/{new_id}")
def supersede_decision(old_id: str, new_id: str):
    """Task 1 — mark old decision superseded by new one (adds graph edge, deactivates old)."""
    require_ready()
    ok = S.dg.memory.supersede(old_id, new_id)
    if not ok: raise HTTPException(404, "one or both decisions not found")
    S.dg.memory.save()
    return {"ok": True}

@app.post("/api/decisions/decay")
def run_decay(days_threshold: int = 90):
    """Task 1 — force a decay pass right now (normally runs auto on first query each day)."""
    require_ready()
    changed = S.dg.memory.decay_confidence(days_threshold=days_threshold)
    S.dg.memory.save()
    return {"changed": changed}

@app.get("/api/companies/patterns")
def cross_company_patterns():
    """Task 5 — anonymised aggregate patterns across all companies."""
    require_ready()
    return {"patterns": S.hub.get_anonymized_patterns()}

@app.get("/api/companies/{company_id}/wisdom")
def company_wisdom(company_id: str, question: str):
    """Task 5 — peer-company wisdom for a specific question."""
    require_ready()
    cm = S.hub.get_company(company_id)
    if not cm: raise HTTPException(404, "company not found")
    wisdom = S.hub.get_wisdom(question, company_id=company_id,
                              embed_model=cm.company_dg.embed_model)
    return {"wisdom": wisdom}

# ── discussion sessions ───────────────────────────────────────────────────────
@app.get("/api/sessions")
def list_sessions():
    require_ready()
    active = None
    if S.dm.active_session:
        a = S.dm.active_session
        active = {
            "id": a.id, "title": a.title,
            "messages": [{"role": m.role, "content": m.content, "ts": m.timestamp} for m in a.messages],
            "company_id": a.company_id,
        }
    return {"active": active, "sessions": S.dm.list_sessions()}

@app.post("/api/sessions/start")
async def start_session(req: Request):
    require_ready()
    body = await req.json()
    sess = S.dm.start_session(
        title=(body.get("title") or "").strip(),
        company_id=(body.get("company_id") or "").strip(),
    )
    return {"id": sess.id, "title": sess.title}

@app.post("/api/sessions/message")
async def chat_message(req: Request):
    require_ready()
    rate_limit("chat", max_n=30, window_s=60)
    global_llm_gate()
    body = await req.json()
    q = (body.get("content") or "").strip()
    if not q: raise HTTPException(400, "content required")
    if not S.dm.active_session:
        S.dm.start_session()
    co_id = S.dm.active_session.company_id
    if co_id:
        cm = S.hub.get_company(co_id)
        # wrap CompanyMemory.query so the hub is passed (Task 5 — wisdom)
        if cm and (cm.company_dg.G is not None or cm.knowledge_dg.G is not None):
            qfn = lambda q, mode="session": cm.query(q, mode=mode, hub=S.hub)
        else:
            qfn = S.dg.query
    else:
        qfn = S.dg.query
    try:
        ans = S.dm.chat(q, S.dg.client, qfn, S.dg.memory, mode="session")
        return {"answer": ans}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))

@app.post("/api/sessions/end")
def end_session():
    require_ready()
    if not S.dm.active_session: raise HTTPException(400, "No active session")
    sess = S.dm.end_session(S.dg.client, memory=S.dg.memory)
    payload = {
        "id": sess.id, "title": sess.title, "summary": sess.summary,
        "key_decisions": sess.key_decisions, "open_questions": sess.open_questions,
        "duration_minutes": sess.duration_minutes, "messages": len(sess.messages),
    }
    # auto-broadcast the session summary to all configured outputs with auto_broadcast=True
    _auto_broadcast_session_async(payload)
    return payload

# ── graph data ────────────────────────────────────────────────────────────────
def _render_kg(G, communities, max_nodes, PAL):
    """Render a knowledge graph (nodes coloured by community, sized by degree)."""
    if G is None or G.number_of_nodes() == 0:
        return [], []
    comms = communities or {}
    if G.number_of_nodes() > max_nodes:
        top = sorted(G.degree(), key=lambda x:x[1], reverse=True)[:max_nodes]
        H = G.subgraph([n for n,_ in top])
    else: H = G
    n2c = {n: cid for cid, ns in comms.items() for n in ns}
    ucids = sorted(set(n2c.get(n, -1) for n in H.nodes()))
    ccol = {c: PAL[i % len(PAL)] for i, c in enumerate(ucids)}
    deg = dict(H.degree()); mx = max(deg.values()) if deg else 1
    nodes, edges = [], []
    for n in H.nodes():
        nodes.append({
            "id": str(n), "label": str(n)[:24],
            "color": ccol.get(n2c.get(n, -1), "#c7bfff"),
            "size": 8 + 24 * (deg.get(n, 0) / mx),
            "title": f"{n}\nCommunity {n2c.get(n,'?')}\nDegree {deg.get(n,0)}",
            "community": n2c.get(n, -1),
        })
    for u, v, data in H.edges(data=True):
        e = {"from": str(u), "to": str(v)}
        # surface useful attrs (call-graph edges carry label='calls' etc.)
        if isinstance(data, dict):
            for k in ("label", "relation", "type", "repo",
                       "src_path", "dst_path"):
                if data.get(k):
                    e[k] = data[k]
        edges.append(e)
    return nodes, edges


@app.get("/api/graph")
def graph_data(scope: str = "knowledge", max_nodes: int = 200, company_id: str | None = None):
    """Returns nodes + edges for the visualizer.

    scope="companies":
        - no company_id → flat overview (one hub per company)
        - with company_id → that company's full knowledge graph
    """
    require_ready()
    import networkx as nx
    PAL = ["#c7bfff","#4ddada","#51df9c","#ffb4ab","#a78bfa","#34d399","#f59e0b","#60a5fa","#f472b6"]

    nodes, edges = [], []

    if scope == "knowledge":
        nodes, edges = _render_kg(S.dg.G, S.dg.communities, max_nodes, PAL)

    elif scope == "decisions":
        decs = S.dg.get_decisions()[:max_nodes]
        for d in decs:
            nodes.append({"id": d["id"], "label": d["question"][:24], "color": "#c7bfff", "size": 14,
                          "title": d["question"]})
        for i,d1 in enumerate(decs):
            for d2 in decs[i+1:]:
                if set(d1.get("communities_used",[])) & set(d2.get("communities_used",[])):
                    edges.append({"from": d1["id"], "to": d2["id"]})

    elif scope == "companies":
        if company_id:
            # render this company's knowledge graph
            cm = S.hub.get_company(company_id)
            if not cm:
                raise HTTPException(404, f"company '{company_id}' not found")
            nodes, edges = _render_kg(cm.dg.G, cm.dg.communities, max_nodes, PAL)
        else:
            # overview: all companies + their docs
            for ci, c in enumerate(S.hub.list_companies()):
                cm = S.hub.get_company(c["id"])
                col = PAL[ci % len(PAL)]
                nodes.append({"id": c["id"], "label": c["name"], "color": col, "size": 22,
                              "title": f"{c['name']} · {len(cm.registry)} docs"})
                for did, doc in list(cm.registry.items())[:max_nodes // max(len(S.hub.companies), 1)]:
                    nid = f"{c['id']}:{did[:12]}"
                    nodes.append({"id": nid, "label": doc["filename"][:20], "color": "#928ea0", "size": 8,
                                  "title": f"{doc['filename']} [{doc['category']}]"})
                    edges.append({"from": c["id"], "to": nid})

    elif scope == "sessions":
        for ss in S.dm.list_sessions()[:max_nodes]:
            sid = ss["id"]
            nodes.append({"id": sid, "label": ss["title"][:24], "color":"#51df9c",
                          "size": 14 + min(ss["messages_count"], 14),
                          "title": f"{ss['title']} · {ss['messages_count']} msgs"})
            for dec in (ss.get("key_decisions") or [])[:3]:
                did = f"D:{sid}:{hash(dec)%10000}"
                nodes.append({"id": did, "label": dec[:18], "color": "#c7bfff", "size": 8, "title": dec})
                edges.append({"from": sid, "to": did})

    return {"nodes": nodes, "edges": edges, "empty": len(nodes) == 0}


# ──────────────────────────────────────────────────────────────────────────────
# MCP SERVER API — exposes the bundled `decisiongraph.mcp_server` for inspection
# and HTTP "try-it" invocation (same handlers an MCP client would hit via stdio)
# ──────────────────────────────────────────────────────────────────────────────
def _mcp_tool_defs_json() -> list[dict]:
    """Read TOOL_DEFS from the bundled MCP module and return them as plain dicts."""
    from decisiongraph import mcp_server as mcp_mod
    out = []
    for t in mcp_mod.TOOL_DEFS:
        out.append({
            "name":         t.name,
            "description":  t.description,
            "inputSchema":  t.inputSchema,
        })
    return out

@app.get("/api/mcp/status")
def mcp_status():
    """Sanity-check that the MCP module imports cleanly and report wiring info."""
    import subprocess
    info = {
        "project_path": str(ROOT),
        "python":       sys.executable,
        "module":       "decisiongraph.mcp_server",
        "active":       False,
        "import_error": None,
        "tools_count":  0,
    }
    try:
        from decisiongraph import mcp_server as mcp_mod
        info["active"] = True
        info["tools_count"] = len(mcp_mod.TOOL_DEFS)
    except Exception as e:
        info["import_error"] = str(e)
    return info

@app.get("/api/mcp/tools")
def mcp_tools():
    return {"tools": _mcp_tool_defs_json()}

@app.get("/api/mcp/config")
def mcp_config():
    """Return Claude Desktop + Claude Code config snippets ready to copy."""
    py = sys.executable.replace("\\", "/")
    cwd = str(ROOT).replace("\\", "/")
    desktop = {
        "mcpServers": {
            "decisiongraph": {
                "command": py,
                "args": ["-m", "decisiongraph.mcp_server"],
                "cwd": cwd,
            }
        }
    }
    cli = f'claude mcp add decisiongraph --command "{py}" --args -m,decisiongraph.mcp_server --cwd "{cwd}"'
    return {"desktop": desktop, "cli": cli, "project_path": cwd, "python": py}

@app.post("/api/mcp/call/{tool_name}")
async def mcp_call(tool_name: str, req: Request):
    """HTTP wrapper that dispatches to the same handler an MCP client would hit.
    Uses the running server's already-loaded S.dg / S.hub / S.dm so the in-memory
    state stays consistent with the rest of the UI."""
    require_ready()
    rate_limit("mcp", max_n=40, window_s=60)
    # tools that hit the LLM go through the global gate
    if tool_name in ("query_knowledge", "ingest_document"):
        global_llm_gate()
    body = await req.json() if (await req.body()) else {}
    from decisiongraph.mcp_server import invoke_tool
    try:
        result = await invoke_tool(tool_name, body or {}, S.dg, S.hub, S.dm)
        return {"tool": tool_name, "result": result}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


# ──────────────────────────────────────────────────────────────────────────────
# SCOPED EXTERNAL-AGENT ACCESS  (Slice-1: grant -> sandbox -> audit)
# Per-workspace (per-company) isolation: the store lives under S.ws.root, so a
# grant physically cannot reach another tenant's data.
# ──────────────────────────────────────────────────────────────────────────────
import os as _os_agent

_SAMPLE_AGENT = _os_agent.path.join(
    _os_agent.path.dirname(__file__), "agentnet", "agents", "summarizer.py")


def _agent_store():
    from decisiongraph.agent_access import AgentAccessStore
    return AgentAccessStore(S.ws.root)


@app.post("/api/agent/grants")
async def agent_mint_grant(req: Request):
    """Mint a scoped, expiring, revocable grant for an external agent."""
    require_ready()
    rate_limit("agent", max_n=30, window_s=60)
    from decisiongraph.agent_access import AgentAccessError
    body = await req.json() if (await req.body()) else {}
    try:
        g = _agent_store().mint(
            agent_name=body.get("agent_name", ""),
            allowed_topics=body.get("allowed_topics", []),
            ttl_seconds=int(body.get("ttl_seconds", 3600)),
            can_write=bool(body.get("can_write", False)),
        )
        return {"grant": g}
    except AgentAccessError as e:
        raise HTTPException(400, str(e))


@app.get("/api/agent/grants")
def agent_list_grants():
    require_ready()
    return {"grants": _agent_store().list_grants()}


@app.post("/api/agent/grants/{token}/revoke")
def agent_revoke_grant(token: str):
    require_ready()
    ok = _agent_store().revoke(token)
    if not ok:
        raise HTTPException(404, "grant not found")
    return {"revoked": True, "token": token}


@app.get("/api/agent/audit")
def agent_audit(limit: int = 200):
    """The audit view: every grant/allow/deny/job event, append-only."""
    require_ready()
    return {"audit": _agent_store().read_audit(limit=limit)}


@app.get("/api/agent/reputation")
def agent_reputation():
    """Phase 2: reputation per agent, computed from the immutable audit log
    (not stored separately) — the agent's standing lives in this company's
    history, nowhere else."""
    require_ready()
    from decisiongraph.agent_reputation import compute_reputation
    return {"reputation": compute_reputation(_agent_store())}


@app.get("/api/network/companies")
def network_companies():
    """Phase 2: anonymized cross-company directory (no names/tokens/content
    cross a tenant boundary — opaque ids + aggregate counts only)."""
    require_ready()
    from decisiongraph.agent_network import network_directory
    return network_directory(WSM.base)


@app.post("/api/agent/grants/revoke_all")
def agent_revoke_all():
    """Governance: revoke every active grant for THIS company."""
    require_ready()
    st = _agent_store()
    n = 0
    for g in st.list_grants():
        if not g.get("revoked") and st.revoke(g["token"]):
            n += 1
    return {"revoked": n}


@app.get("/api/agent/audit/search")
def agent_audit_search(q: str = "", limit: int = 500):
    """Governance: substring search over THIS company's audit log."""
    require_ready()
    ql = (q or "").strip().lower()
    rows = _agent_store().read_audit(limit=limit)
    if ql:
        rows = [r for r in rows
                if ql in json.dumps(r, default=str).lower()]
    return {"audit": rows, "query": q}


# ──────────────────────────────────────────────────────────────────────────────
# HTTP MCP endpoints — external agents (Claude/Cursor/anything) connect here
# ──────────────────────────────────────────────────────────────────────────────
def _bearer(req: Request):
    h = req.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    return h[7:].strip() or None


async def _mcp_http_dispatch(req: Request, ws_token: str, owner_mode: bool):
    token = _bearer(req)
    if not token:
        raise HTTPException(401, "missing Authorization: Bearer header")
    if not WSM._safe(ws_token):
        raise HTTPException(400, "invalid workspace id")
    ws = WSM.get(ws_token)
    if ws is None:
        raise HTTPException(404, "workspace not found")

    from decisiongraph.agent_access import AgentAccessStore
    store = AgentAccessStore(ws.root)
    grant = store.get(token)
    if not grant:
        raise HTTPException(403, "unknown or invalid token")
    if owner_mode and not grant.get("is_owner"):
        raise HTTPException(403, "owner endpoint requires an owner token")
    if (not owner_mode) and grant.get("is_owner"):
        raise HTTPException(400, "owner token sent to agent endpoint")

    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        raise HTTPException(400, "expected JSON-RPC 2.0 request")

    from decisiongraph.agent_job import WorkspaceMemoryAdapter
    from decisiongraph.mcp_gateway import MCPGateway
    # bind the workspace so per-request dg/hub/dm work for tool dispatch
    tok_ctx = _CURRENT_WS.set(ws)
    try:
        gw = MCPGateway(
            store=store,
            adapter=WorkspaceMemoryAdapter(ws.dg),
            agent_token=token,
            job_id=f"ext_{int(time.time()*1000)}",
            dg=ws.dg,
            owner=bool(grant.get("is_owner")),
            ws=ws,
            ctx={"JOBS": JOBS, "S_simulator": S.simulator,
                 "settings": S.settings})
        return gw.handle(body)
    finally:
        _CURRENT_WS.reset(tok_ctx)


@app.post("/api/mcp/v1/owner/{ws_token}")
async def mcp_http_owner(ws_token: str, req: Request):
    """Owner MCP endpoint — full DG tool surface."""
    return await _mcp_http_dispatch(req, ws_token, owner_mode=True)


@app.post("/api/mcp/v1/agent/{ws_token}")
async def mcp_http_agent(ws_token: str, req: Request):
    """Hired-agent MCP endpoint — scope-gated, read-mostly."""
    return await _mcp_http_dispatch(req, ws_token, owner_mode=False)


@app.post("/api/agent/connect_owner")
async def agent_connect_owner(req: Request):
    """Mint an OWNER MCP token for THIS workspace + return the connection
    bundle: URL, header, claude mcp add command."""
    require_ready()
    body = await req.json() if (await req.body()) else {}
    name = (body.get("name") or "owner-mcp").strip()
    ttl = int(body.get("ttl_seconds", 86400))
    g = _agent_store().mint(agent_name=name, allowed_topics=[],
                             ttl_seconds=ttl, can_write=True, is_owner=True)
    ws = S.ws
    base = str(req.base_url).rstrip("/")
    url = f"{base}/api/mcp/v1/owner/{ws.token}"
    return {
        "grant": g,
        "connection": {
            "url": url,
            "auth_header": f"Bearer {g['token']}",
            "claude_mcp_add": (
                f'claude mcp add decisiongraph-owner '
                f'--transport http '
                f'--header "Authorization: Bearer {g["token"]}" '
                f'{url}'),
            "curl_example": (
                f'curl -X POST -H "Authorization: Bearer {g["token"]}" '
                f'-H "Content-Type: application/json" '
                f'-d \'{{"jsonrpc":"2.0","id":1,"method":"tools/list"}}\' '
                f'{url}'),
        },
    }


@app.post("/api/agent/connect_agent")
async def agent_connect_agent(req: Request):
    """Mint a SCOPED AGENT MCP token + connection bundle."""
    require_ready()
    body = await req.json() if (await req.body()) else {}
    name = (body.get("name") or "external-agent").strip()
    topics = body.get("allowed_topics") or []
    ttl = int(body.get("ttl_seconds", 3600))
    if not topics:
        raise HTTPException(400, "allowed_topics required (scope must be non-empty)")
    g = _agent_store().mint(agent_name=name, allowed_topics=topics,
                             ttl_seconds=ttl, can_write=False, is_owner=False)
    ws = S.ws
    base = str(req.base_url).rstrip("/")
    url = f"{base}/api/mcp/v1/agent/{ws.token}"
    return {
        "grant": g,
        "connection": {
            "url": url,
            "auth_header": f"Bearer {g['token']}",
            "claude_mcp_add": (
                f'claude mcp add decisiongraph-agent '
                f'--transport http '
                f'--header "Authorization: Bearer {g["token"]}" '
                f'{url}'),
            "curl_example": (
                f'curl -X POST -H "Authorization: Bearer {g["token"]}" '
                f'-H "Content-Type: application/json" '
                f'-d \'{{"jsonrpc":"2.0","id":1,"method":"tools/list"}}\' '
                f'{url}'),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# PROJECTS · multiple named workspaces per device
# ──────────────────────────────────────────────────────────────────────────────
def _device_id(req: Request) -> Optional[str]:
    return req.cookies.get(DEVICE_COOKIE)


@app.get("/api/projects")
def projects_list(req: Request):
    """List this device's projects + which one is currently active."""
    from decisiongraph import projects as _proj
    dev = _device_id(req)
    items = _proj.list_projects(dev) if dev else []
    active = req.cookies.get(PROJECT_COOKIE)
    return {"device_id": dev, "active": active, "projects": items}


@app.post("/api/projects")
async def projects_create(req: Request):
    """Create a new project. Returns the project record; UI should then
    redirect / POST /api/projects/{id}/switch to make it active."""
    from decisiongraph import projects as _proj
    dev = _device_id(req)
    if not dev:
        # middleware should have set this — sanity guard
        raise HTTPException(400, "no device cookie; refresh the page once first")
    body = await req.json() if (await req.body()) else {}
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    p = _proj.create_project(dev, name, WSM)
    return {"project": p}


@app.post("/api/projects/{project_id}/switch")
def projects_switch(project_id: str, req: Request):
    """Set the active project for this device. Returns the project."""
    from decisiongraph import projects as _proj
    dev = _device_id(req)
    p = _proj.get_project(dev, project_id) if dev else None
    if not p:
        raise HTTPException(404, "project not found")
    resp = JSONResponse({"project": p, "switched": True})
    resp.set_cookie(PROJECT_COOKIE, p["id"], max_age=60 * 60 * 24 * 365,
                    httponly=False, samesite="lax")
    return resp


@app.post("/api/projects/{project_id}/rename")
async def projects_rename(project_id: str, req: Request):
    from decisiongraph import projects as _proj
    dev = _device_id(req)
    body = await req.json() if (await req.body()) else {}
    name = body.get("name") or ""
    p = _proj.rename_project(dev, project_id, name)
    if not p:
        raise HTTPException(404, "project not found or invalid name")
    return {"project": p}


@app.post("/api/projects/{project_id}/delete")
def projects_delete(project_id: str, req: Request):
    """Soft-delete. Underlying workspace storage is preserved on disk."""
    from decisiongraph import projects as _proj
    dev = _device_id(req)
    ok = _proj.delete_project(dev, project_id) if dev else False
    if not ok:
        raise HTTPException(404, "project not found")
    return {"deleted": True}


@app.get("/projects", response_class=HTMLResponse)
def projects_page():
    p = _os_agent.path.join(_os_agent.path.dirname(__file__),
                            "agentnet", "ui", "projects.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "projects page not found")


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 4 — Marketplace pivot: sellers · listings · reviews · coordinator
# ──────────────────────────────────────────────────────────────────────────────

# A · Seller signup ─────────────────────────────────────────────────────────
@app.post("/api/sellers")
async def sellers_signup(req: Request):
    from decisiongraph import sellers as _s
    body = await req.json() if (await req.body()) else {}
    try:
        rec = _s.signup(
            display_name=body.get("display_name") or "",
            email=body.get("email"), bio=body.get("bio"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"seller": rec, "note": "Store the seller_key — it's needed to manage your listings."}


@app.get("/api/sellers")
def sellers_list():
    from decisiongraph import sellers as _s
    return {"sellers": _s.list_all()}


@app.get("/api/sellers/{sid}")
def sellers_get(sid: str):
    from decisiongraph import sellers as _s
    rec = _s.get(sid)
    if not rec:
        raise HTTPException(404, "seller not found")
    return rec


def _seller_from_key(req: Request):
    """Auth helper: returns the seller record if header X-Seller-Key matches."""
    from decisiongraph import sellers as _s
    key = req.headers.get("x-seller-key") or req.headers.get("X-Seller-Key")
    if not key:
        raise HTTPException(401, "X-Seller-Key header required")
    rec = _s.authenticate(key)
    if not rec:
        raise HTTPException(403, "invalid seller key")
    return rec


@app.post("/api/sellers/me/update")
async def sellers_update_me(req: Request):
    from decisiongraph import sellers as _s
    seller = _seller_from_key(req)
    body = await req.json() if (await req.body()) else {}
    out = _s.update(seller["id"],
                     bio=body.get("bio"), email=body.get("email"),
                     display_name=body.get("display_name"))
    return {"seller": out}


# B · Listings ──────────────────────────────────────────────────────────────
@app.post("/api/listings")
async def listings_submit(req: Request):
    from decisiongraph import listings as _l
    seller = _seller_from_key(req)
    body = await req.json() if (await req.body()) else {}
    try:
        rec = _l.submit(
            seller_id=seller["id"],
            name=body.get("name") or "",
            description=body.get("description") or "",
            tags=body.get("tags") or [],
            suggested_topics=body.get("suggested_topics") or [],
            webhook_url=body.get("webhook_url") or "",
            expected_scope_hint=body.get("expected_scope_hint") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"listing": rec}


@app.get("/api/listings")
def listings_public(seller_id: Optional[str] = None):
    from decisiongraph import listings as _l, sellers as _s
    if seller_id:
        items = _l.list_by_seller(seller_id)
    else:
        items = _l.list_public()
    # join seller display_name for UI
    for it in items:
        sel = _s.get(it.get("seller_id") or "")
        it["seller_name"] = (sel or {}).get("display_name") or "unknown"
    return {"listings": items}


@app.get("/api/listings/{lid}")
def listings_get(lid: str):
    from decisiongraph import listings as _l, sellers as _s, reviews as _r
    rec = _l.get(lid)
    if not rec:
        raise HTTPException(404, "listing not found")
    seller = _s.get(rec.get("seller_id") or "")
    return {
        "listing": rec,
        "seller": seller,
        "average_rating": _l.average_rating(rec),
        "reviews": _r.for_listing(lid),
    }


@app.post("/api/listings/{lid}/update")
async def listings_update(lid: str, req: Request):
    from decisiongraph import listings as _l
    seller = _seller_from_key(req)
    rec = _l.get(lid)
    if not rec:
        raise HTTPException(404, "not found")
    if rec.get("seller_id") != seller["id"]:
        raise HTTPException(403, "not your listing")
    body = await req.json() if (await req.body()) else {}
    try:
        out = _l.update(lid, **{
            k: body[k] for k in
            ("description", "tags", "suggested_topics", "webhook_url",
             "expected_scope_hint")
            if k in body})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"listing": out}


@app.post("/api/listings/{lid}/delete")
def listings_delete(lid: str, req: Request):
    from decisiongraph import listings as _l
    seller = _seller_from_key(req)
    rec = _l.get(lid)
    if not rec:
        raise HTTPException(404, "not found")
    if rec.get("seller_id") != seller["id"]:
        raise HTTPException(403, "not your listing")
    _l.delete(lid)
    return {"deleted": True}


# D · External hire flow ────────────────────────────────────────────────────
@app.post("/api/listings/{lid}/hire")
async def listings_hire(lid: str, req: Request):
    """Hire a third-party listing.

    Mints a scoped grant for the buyer's current workspace and POSTs a hire
    request to the seller's webhook. The seller's agent is expected to
    connect to the buyer's MCP endpoint with that grant, do its work,
    and reply (synchronous) with a final answer + citation. We persist the
    answer as a [external-agent:<listing_name>] decision in the buyer's DG.
    """
    require_ready()
    rate_limit("agent", max_n=10, window_s=60)
    from decisiongraph import listings as _l
    body = await req.json() if (await req.body()) else {}
    listing = _l.get(lid)
    if not listing or not listing.get("approved"):
        raise HTTPException(404, "listing not found or not approved")
    topics = body.get("topics") or listing.get("suggested_topics") or []
    if not topics:
        raise HTTPException(400, "topics required")
    task = (body.get("task") or "do the work").strip()
    ttl = int(body.get("ttl_seconds") or 900)

    store = _agent_store()
    grant = store.mint(agent_name=listing["name"], allowed_topics=topics,
                        ttl_seconds=ttl, can_write=False)
    base = str(req.base_url).rstrip("/")
    buyer_mcp = f"{base}/api/mcp/v1/agent/{S.ws.token}"

    import time as _t, urllib.request as _ur, urllib.error as _ue, json as _j
    started = _t.time()
    hire_payload = {
        "version": "1",
        "hire_id": grant["token"][:12],
        "listing_id": lid,
        "listing_name": listing["name"],
        "task": task,
        "allowed_topics": topics,
        "buyer_mcp": {
            "url": buyer_mcp,
            "bearer": grant["token"],
            "expires_at": grant["expires_at"],
        },
        "callback_hint": f"{base}/api/listings/{lid}/hire/result",
    }
    req_body = _j.dumps(hire_payload).encode("utf-8")
    hreq = _ur.Request(
        listing["webhook_url"], data=req_body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "DecisionGraph-Hire/1"})
    try:
        with _ur.urlopen(hreq, timeout=int(body.get("webhook_timeout") or 180)) as r:
            resp_text = r.read().decode("utf-8", "replace")
            resp_status = r.status
    except _ue.HTTPError as e:
        try: store.revoke(grant["token"])
        except Exception: pass
        raise HTTPException(502, f"seller webhook returned {e.code}: "
                                   f"{e.read().decode('utf-8','replace')[:200]}")
    except Exception as e:
        try: store.revoke(grant["token"])
        except Exception: pass
        raise HTTPException(502, f"seller webhook unreachable: "
                                   f"{type(e).__name__}: {e}")
    try:
        parsed = _j.loads(resp_text or "{}")
    except Exception:
        parsed = {"answer": resp_text[:2000], "citation": "raw seller response"}

    duration = round(_t.time() - started, 3)
    final_answer = (parsed.get("answer") or "").strip() or "(seller returned empty)"
    citation = parsed.get("citation") or f"external listing {lid}"

    # persist the answer as a decision in buyer's DG
    try:
        S.dg.memory.store(
            question=f"[external-agent:{listing['name']}] {task}"[:500],
            answer=final_answer[:4000],
            reasoning_summary=(
                f"External hire: listing={lid}, seller={listing.get('seller_id')}, "
                f"duration={duration}s, scope={topics}"),
            communities_used=[], context_triples=[])
        S.dg.memory.save()
    except Exception:
        pass

    # revoke the grant after the call (single-use hire)
    try: store.revoke(grant["token"])
    except Exception: pass

    _l.bump_hire(lid)

    return {
        "hire": {
            "listing_id": lid, "listing_name": listing["name"],
            "via": "external", "duration_s": duration,
            "seller_status": resp_status,
            "buyer_mcp": buyer_mcp,
        },
        "answer": final_answer,
        "citation": citation,
        "raw": parsed,
    }


# E · Reviews ───────────────────────────────────────────────────────────────
@app.post("/api/listings/{lid}/reviews")
async def review_submit(lid: str, req: Request):
    from decisiongraph import reviews as _r
    body = await req.json() if (await req.body()) else {}
    try:
        rec = _r.submit(lid,
                         buyer_workspace=S.ws.token,
                         stars=float(body.get("stars") or 5),
                         comment=body.get("comment") or "",
                         hire_job_id=body.get("hire_job_id"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"review": rec}


@app.get("/api/listings/{lid}/reviews")
def review_list(lid: str):
    from decisiongraph import reviews as _r
    return {"reviews": _r.for_listing(lid)}


# G · Coordinator orchestration ─────────────────────────────────────────────
@app.post("/api/orchestration/coordinator")
async def orchestration_coordinator(req: Request):
    """Autonomous coordinator: one LLM manager plans + hires sub-agents from
    the catalog dynamically to satisfy a goal."""
    require_ready()
    rate_limit("agent", max_n=3, window_s=120)
    from decisiongraph.orchestration import run_coordinator
    from decisiongraph.agent_job import WorkspaceMemoryAdapter
    body = await req.json() if (await req.body()) else {}
    goal = (body.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "goal required")
    try:
        return {"run": run_coordinator(
            store=_agent_store(),
            adapter=WorkspaceMemoryAdapter(S.dg),
            ws_dg=S.dg, goal=goal,
            allowed_agent_ids=body.get("allowed_agent_ids"),
            max_hires=int(body.get("max_hires") or 5),
            max_seconds=int(body.get("max_seconds") or 300),
            allowed_topics=body.get("allowed_topics"))}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


# Pages
@app.get("/sellers", response_class=HTMLResponse)
def sellers_page():
    p = _os_agent.path.join(_os_agent.path.dirname(__file__),
                            "agentnet", "ui", "sellers.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "sellers page not found")


@app.get("/marketplace/agent/{lid}", response_class=HTMLResponse)
def listing_profile_page(lid: str):
    """Profile page for any listing — pulls data client-side via API."""
    p = _os_agent.path.join(_os_agent.path.dirname(__file__),
                            "agentnet", "ui", "listing_profile.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "listing profile page not found")


# Catalog endpoint now also surfaces approved third-party listings
@app.get("/api/marketplace/agents")
def marketplace_list():
    """Catalog of agents available for hire — bundled scripts + approved
    third-party listings. Each entry is shaped uniformly for the UI; the
    `kind` field tells the marketplace whether to use the bundled hire flow
    (`/api/marketplace/hire`) or the external hire flow
    (`/api/listings/{id}/hire`)."""
    require_ready()
    from decisiongraph.agent_catalog import load_catalog
    from decisiongraph import listings as _l, sellers as _s
    pub = []
    for a in load_catalog():
        pub.append({
            "id": a["id"],
            "name": a["name"],
            "description": a["description"],
            "suggested_topics": a["suggested_topics"],
            "tags": list(a.get("tags") or []) + ["bundled"],
            "available": a["available"],
            "kind": "bundled",
            "seller_name": "DecisionGraph",
        })
    for it in _l.list_public():
        sel = _s.get(it.get("seller_id") or "")
        avg = _l.average_rating(it)
        pub.append({
            "id": it["id"],
            "name": it["name"],
            "description": it["description"],
            "suggested_topics": it.get("suggested_topics") or [],
            "tags": list(it.get("tags") or []) + ["third-party"],
            "available": True,
            "kind": "external",
            "seller_name": (sel or {}).get("display_name") or "unknown",
            "seller_id": it.get("seller_id"),
            "rating": avg,
            "rating_count": it.get("rating_count") or 0,
            "hire_count": it.get("hire_count") or 0,
        })
    return {"agents": pub}


@app.post("/api/marketplace/hire")
async def marketplace_hire(req: Request):
    """Phase 3 hire flow: pick a catalog agent, give it a scope + task, and
    the server mints a one-shot grant, runs the agent in the sandbox against
    THIS company's DecisionGraph, writes the learning back, revokes the
    token, and returns the job. Reuses the Phase 1 audited lifecycle."""
    require_ready()
    rate_limit("agent", max_n=10, window_s=60)
    from decisiongraph.agent_catalog import get_agent, CatalogError
    from decisiongraph.agent_job import (
        run_agent_job, run_agent_job_mcp, run_agent_job_react,
        WorkspaceMemoryAdapter, AgentJobError)
    body = await req.json() if (await req.body()) else {}
    agent_id = body.get("agent_id", "")
    topics = body.get("allowed_topics") or body.get("topics") or []
    task = body.get("task", "do the work")
    ttl = int(body.get("ttl_seconds", 600))
    if not agent_id or not topics:
        raise HTTPException(400, "agent_id and allowed_topics required")
    try:
        a = get_agent(agent_id)
    except CatalogError as e:
        raise HTTPException(404, str(e))
    store = _agent_store()
    grant = store.mint(agent_name=a["name"], allowed_topics=topics,
                       ttl_seconds=ttl, can_write=False)
    tags = a.get("tags") or []
    use_react = "via-react" in tags
    use_mcp = "via-mcp" in tags
    try:
        if use_react:
            summary = run_agent_job_react(
                store=store, ws_dg=S.dg, agent_token=grant["token"],
                topics=topics, task=task)
        else:
            runner = run_agent_job_mcp if use_mcp else run_agent_job
            summary = runner(
                store=store, adapter=WorkspaceMemoryAdapter(S.dg),
                agent_token=grant["token"], topics=topics,
                agent_script=a["script_path"], task=task)
        via = "react" if use_react else ("mcp" if use_mcp else "staged")
        return {"hire": {"agent_id": agent_id, "agent_name": a["name"],
                         "via": via},
                "job": summary}
    except AgentJobError as e:
        raise HTTPException(422, str(e))


@app.post("/api/orchestration/pipeline")
async def orchestration_pipeline(req: Request):
    """Run a multi-agent pipeline against THIS company's DecisionGraph.
    body: { stages: [{ agent_id, topics:[str], task:str,
                       pass_output_to_next?: bool }] }
    """
    require_ready()
    rate_limit("agent", max_n=5, window_s=60)
    from decisiongraph.orchestration import run_pipeline
    from decisiongraph.agent_job import WorkspaceMemoryAdapter
    body = await req.json() if (await req.body()) else {}
    stages = body.get("stages") or []
    if not isinstance(stages, list) or not stages:
        raise HTTPException(400, "stages required (list)")
    try:
        return {"run": run_pipeline(_agent_store(),
                                    WorkspaceMemoryAdapter(S.dg),
                                    stages)}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


@app.post("/api/orchestration/coordinator")
async def orchestration_coordinator(req: Request):
    """Stub: coordinator mode is next on the roadmap."""
    raise HTTPException(501, "coordinator mode not yet implemented")


@app.post("/api/orchestration/dialog")
async def orchestration_dialog(req: Request):
    """Run a multi-agent dialog. Each member runs in its own sandbox,
    speaks MCP over stdio, exchanges messages with peers via a shared bus.

    body: { goal: str, max_rounds?: int,
            members: [{ agent_id, agent_role?, topics:[str], task:str }] }
    """
    require_ready()
    rate_limit("agent", max_n=3, window_s=120)
    from decisiongraph.orchestration import run_dialog
    from decisiongraph.agent_job import WorkspaceMemoryAdapter
    body = await req.json() if (await req.body()) else {}
    members = body.get("members") or []
    goal = body.get("goal") or ""
    max_rounds = int(body.get("max_rounds") or 3)
    if not isinstance(members, list) or len(members) < 2:
        raise HTTPException(400, "dialog needs >=2 members")
    if max_rounds < 1 or max_rounds > 10:
        raise HTTPException(400, "max_rounds must be 1..10")
    try:
        return {"run": run_dialog(_agent_store(),
                                   WorkspaceMemoryAdapter(S.dg), S.dg,
                                   members, goal=goal,
                                   max_rounds=max_rounds)}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


@app.post("/api/orchestration/swarm")
async def orchestration_swarm(req: Request):
    """Run N agents IN PARALLEL against THIS company's DecisionGraph.
    Each gets its own scoped grant + sandbox + audit. All write back to
    the same DG. Returns when all have finished (or a member timed out).

    body: { goal?: str,
            members: [{ agent_id, topics:[str], task:str }, ...] }
    """
    require_ready()
    rate_limit("agent", max_n=5, window_s=60)
    from decisiongraph.orchestration import run_swarm
    from decisiongraph.agent_job import WorkspaceMemoryAdapter
    body = await req.json() if (await req.body()) else {}
    members = body.get("members") or []
    goal = body.get("goal") or ""
    if not isinstance(members, list) or not members:
        raise HTTPException(400, "members required (non-empty list)")
    try:
        return {"run": run_swarm(_agent_store(),
                                  WorkspaceMemoryAdapter(S.dg),
                                  S.dg,
                                  members,
                                  goal=goal)}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


@app.get("/api/orchestration/runs")
def orchestration_runs(limit: int = 50):
    require_ready()
    from decisiongraph.orchestration import list_runs
    return {"runs": list_runs(_agent_store(), limit=limit)}


@app.get("/api/orchestration/runs/{run_id}")
def orchestration_run_get(run_id: str):
    require_ready()
    from decisiongraph.orchestration import get_run
    r = get_run(_agent_store(), run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return r


@app.get("/orchestration", response_class=HTMLResponse)
def orchestration_page():
    p = _os_agent.path.join(_os_agent.path.dirname(__file__),
                            "agentnet", "ui", "orchestration.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "orchestration page not found")


@app.get("/docs", response_class=HTMLResponse)
def docs_page():
    p = _os_agent.path.join(_os_agent.path.dirname(__file__),
                            "agentnet", "ui", "docs.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "docs page not found")


@app.get("/marketplace", response_class=HTMLResponse)
def marketplace_page():
    p = _os_agent.path.join(_os_agent.path.dirname(__file__),
                            "agentnet", "ui", "marketplace.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "marketplace not found")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    p = _os_agent.path.join(_os_agent.path.dirname(__file__),
                            "agentnet", "ui", "dashboard.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "dashboard not found")


@app.post("/api/agent/run")
async def agent_run(req: Request):
    """Run the bundled sample agent through the full audited lifecycle.
    Only the bundled, vetted agent script is runnable via the API (no
    arbitrary code path is accepted from the request)."""
    require_ready()
    rate_limit("agent", max_n=10, window_s=60)
    from decisiongraph.agent_job import (
        run_agent_job, WorkspaceMemoryAdapter, AgentJobError)
    body = await req.json() if (await req.body()) else {}
    token = body.get("agent_token", "")
    topics = body.get("topics", [])
    task = body.get("task", "summarize the scoped knowledge")
    if not token or not topics:
        raise HTTPException(400, "agent_token and topics required")
    try:
        summary = run_agent_job(
            store=_agent_store(),
            adapter=WorkspaceMemoryAdapter(S.dg),
            agent_token=token,
            topics=topics,
            agent_script=_SAMPLE_AGENT,
            task=task,
        )
        return {"job": summary}
    except AgentJobError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


@app.get("/agents", response_class=HTMLResponse)
def agents_page():
    p = _os_agent.path.join(_os_agent.path.dirname(__file__),
                            "agentnet", "ui", "agents.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "agents page not found")


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_page():
    p = _os_agent.path.join(_os_agent.path.dirname(__file__),
                            "agentnet", "ui", "onboarding.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "onboarding page not found")


# ──────────────────────────────────────────────────────────────────────────────
# INTEGRATIONS API
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/integrations")
def list_integrations():
    return {"integrations": S.integrations.list_all()}


# ── Onboarding Companion: one endpoint for the dedicated /onboarding page ──
@app.get("/api/onboarding/brief")
async def onboarding_brief(req: Request):
    """Aggregates everything a new joiner (or new AI session) needs:
    repo overview, active topics, recent decisions, recent AI edits,
    agent reputation. Reuses the get_onboarding_brief MCP tool internally."""
    require_ready()
    from decisiongraph.mcp_server import invoke_tool
    qp = dict(req.query_params)
    args = {
        "topic":           qp.get("topic", ""),
        "limit_decisions": int(qp.get("limit_decisions", 8)),
        "limit_edits":     int(qp.get("limit_edits", 6)),
        "limit_topics":    int(qp.get("limit_topics", 5)),
    }
    try:
        return await invoke_tool("get_onboarding_brief", args, S.dg, S.hub, S.dm)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))


@app.get("/api/integrations/git-hook")
def get_git_hook_script(req: Request):
    """Returns a ready-to-install bash post-commit hook script.
    User runs ONE command on their dev machine and every git commit
    gets posted to DG as a captured decision."""
    require_ready()
    base = str(req.base_url).rstrip("/")
    ws = _CURRENT_WS.get()
    token = getattr(ws, "token", "")
    script = f"""#!/bin/bash
# DecisionGraph post-commit hook — auto-capture every commit into your DG
# Install:  curl -fsSL "{base}/api/integrations/git-hook" > .git/hooks/post-commit && chmod +x .git/hooks/post-commit
set -e
SHA=$(git rev-parse HEAD)
SUBJECT=$(git log -1 --pretty=%s "$SHA")
BODY=$(git log -1 --pretty=%b "$SHA")
AUTHOR=$(git log -1 --pretty=%an "$SHA")
DIFF=$(git diff-tree --no-commit-id --stat -r "$SHA" 2>/dev/null | head -40 || true)
REPO=$(basename "$(git rev-parse --show-toplevel)")

# Build JSON safely with jq if available, else python
if command -v jq >/dev/null 2>&1; then
  BODY_JSON=$(jq -aRs '.' <<< "$BODY")
  DIFF_JSON=$(jq -aRs '.' <<< "$DIFF")
  SUB_JSON=$(jq -aRs '.' <<< "$SUBJECT")
else
  BODY_JSON=$(python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" <<< "$BODY")
  DIFF_JSON=$(python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" <<< "$DIFF")
  SUB_JSON=$(python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" <<< "$SUBJECT")
fi

curl -fsS -X POST "{base}/api/mcp/call/track_decision" \\
  -H "Content-Type: application/json" \\
  -H "Cookie: dg_ws={token}" \\
  -d "{{\\"question\\":\\"[commit:$AUTHOR] $REPO\\",\\"answer\\":$SUB_JSON,\\"reasoning\\":$BODY_JSON,\\"topic\\":\\"$REPO\\"}}" \\
  >/dev/null 2>&1 || true   # never block the commit
"""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(script, media_type="text/x-shellscript",
                              headers={"Content-Disposition":
                                       'inline; filename="post-commit"'})

@app.get("/api/integrations/{name}/config")
def get_integration_config(name: str):
    return {"config": S.integrations.configs.get(name, {})}

@app.post("/api/integrations/{name}/config")
async def set_integration_config(name: str, req: Request):
    body = await req.json()
    try:
        S.integrations.configure(name, body or {})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.delete("/api/integrations/{name}/config")
def clear_integration(name: str):
    S.integrations.clear(name)
    return {"ok": True}

@app.post("/api/integrations/{name}/health")
def integration_health(name: str):
    r = S.integrations.health(name)
    return r.to_dict()

@app.post("/api/integrations/{name}/sync")
async def integration_sync(name: str, req: Request):
    """Pull docs from an input integration and ingest into the personal graph,
    or — by default — into the integration's configured `default_company_id`.
    Pass `company_id` in the body to override per-call (use "" to force personal)."""
    require_ready()
    body = await req.json()
    limit = int(body.get("limit", 25))

    # Resolution order: explicit body.company_id (incl. ""), else integration's default_company_id
    if "company_id" in body:
        company_id = body.get("company_id") or None
    else:
        company_id = S.integrations.default_company_for(name) or None

    extra = {k: v for k, v in body.items() if k not in ("limit", "company_id")}

    ingest_fn = S.dg.ingest
    target_label = "personal graph"
    if company_id:
        cm = S.hub.get_company(company_id)
        if not cm: raise HTTPException(404, f"company '{company_id}' not found")
        ingest_fn = cm.ingest
        target_label = f"company '{cm.company_name}'"
    try:
        out = S.integrations.ingest_into(name, ingest_fn, limit=limit, **extra)
        out["routed_to"] = target_label
        out["company_id"] = company_id or ""
        return out
    except NotImplementedError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, str(e))

@app.post("/api/integrations/broadcast/decision/{decision_id}")
def broadcast_decision(decision_id: str):
    require_ready()
    decs = S.dg.get_decisions()
    d = next((x for x in decs if x.get("id") == decision_id), None)
    if not d: raise HTTPException(404, "decision not found")
    return {"results": S.integrations.broadcast_decision(d)}

@app.post("/api/integrations/broadcast/session")
async def broadcast_session(req: Request):
    require_ready()
    body = await req.json()
    sid = body.get("session_id")
    if not sid: raise HTTPException(400, "session_id required")
    sess = S.dm.get_session(sid)
    if not sess: raise HTTPException(404, "session not found")
    return {"results": S.integrations.broadcast_session(sess)}


# ──────────────────────────────────────────────────────────────────────────────
def main():
    """Console-script entry-point for `dg-server`."""
    print("DecisionGraph server starting on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
