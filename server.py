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

JOBS.register("dream", _job_dream)
JOBS.register("ingest", _job_ingest)
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
app = FastAPI(title="DecisionGraph")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

WS_COOKIE = "dg_ws"

# Paths that must NOT mint/require a workspace (static + health)
_NO_WS_PREFIXES = ("/favicon", "/static")

@app.middleware("http")
async def bind_workspace(request: Request, call_next):
    """Resolve this visitor's isolated workspace from cookie / header / query,
    minting a fresh one on first contact. Bind it for the request so every
    `S.dg/hub/dm` access hits only this visitor's data."""
    path = request.url.path
    if path in ("/favicon.ico",) or any(path.startswith(p) for p in _NO_WS_PREFIXES):
        return await call_next(request)

    token = (request.headers.get("X-Workspace")
             or request.query_params.get("ws")
             or request.cookies.get(WS_COOKIE))
    minted = False
    if not token or not WorkspaceManager._safe(token):
        token = WorkspaceManager.new_token()
        minted = True

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
    for u, v in H.edges():
        edges.append({"from": str(u), "to": str(v)})
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
# INTEGRATIONS API
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/integrations")
def list_integrations():
    return {"integrations": S.integrations.list_all()}

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
if __name__ == "__main__":
    print("DecisionGraph server starting on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
