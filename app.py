"""DecisionGraph — Enterprise Memory OS  |  Streamlit UI v3.0 + Stitch UI"""
import streamlit as st
import os, sys, tempfile, json, time
import networkx as nx
import streamlit.components.v1 as components
from pathlib import Path

# ── credential persistence ─────────────────────────────────────────────────────
_CREDS_FILE = Path(__file__).parent / ".dg_credentials.json"

def _save_creds(api_key, base_url, model):
    try:
        _CREDS_FILE.write_text(json.dumps({
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
        }))
    except Exception:
        pass

def _load_creds():
    try:
        if _CREDS_FILE.exists():
            return json.loads(_CREDS_FILE.read_text())
    except Exception:
        pass
    return None

# ── load Stitch HTML screens ───────────────────────────────────────────────────
_STITCH_DIR = Path(__file__).parent / "stitch_ui"

def load_stitch(filename: str) -> str:
    """Read a Stitch HTML file. Returns empty string if not found."""
    p = _STITCH_DIR / filename
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""

STITCH_KNOWLEDGE  = load_stitch("knowledge_base.html")
STITCH_DISCUSSION = load_stitch("discussion_chat.html")
STITCH_QUERY      = load_stitch("query_mode.html")
STITCH_GRAPH      = load_stitch("graph_visualizer.html")

st.set_page_config(
    page_title="DecisionGraph",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="collapsed",   # sidebar hidden on first load
)

# ── CSS ────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;700;800&family=Inter:wght@400;500;600&display=swap');

:root{
  --bg:#0a0a0f; --surf:#111118; --surf2:#16161f;
  --border:#252535; --accent:#7c6af7; --teal:#3ecfcf;
  --text:#e8e8f0; --muted:#6b6b80;
  --green:#3ecf8e; --warn:#f7a94a; --red:#f76a6a;
}
*{box-sizing:border-box;}
html,body,.stApp{background:var(--bg)!important;color:var(--text);}
#MainMenu,footer,header{visibility:hidden;}
.stDeployButton{display:none;}
*{font-family:'Inter',sans-serif;}
h1,h2,h3{font-family:'Syne',sans-serif!important;}
.mono{font-family:'Space Mono',monospace!important;}
::-webkit-scrollbar{width:5px;}
::-webkit-scrollbar-track{background:var(--bg);}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px;}

/* sidebar */
[data-testid="stSidebar"]{background:#0e0e16!important;border-right:1px solid var(--border)!important;}

/* all text inputs */
input,textarea,.stTextInput input,.stTextArea textarea{
  background:#1a1a26!important;
  border:1px solid var(--border)!important;
  border-radius:8px!important;
  color:var(--text)!important;
  font-size:.9rem!important;
}
input:focus,textarea:focus{
  border-color:var(--accent)!important;
  box-shadow:0 0 0 3px rgba(124,106,247,.18)!important;
  outline:none!important;
}
/* selectbox */
[data-baseweb="select"] > div{
  background:#1a1a26!important;border:1px solid var(--border)!important;border-radius:8px!important;
}
/* file uploader */
[data-testid="stFileUploader"]{background:#1a1a26!important;border:1.5px dashed var(--border)!important;border-radius:10px!important;}
/* progress */
.stProgress>div>div{background:linear-gradient(90deg,var(--accent),var(--teal))!important;}
/* radio */
.stRadio [data-testid="stMarkdownContainer"]{color:var(--text)!important;}

/* ALL buttons */
.stButton>button{
  background:linear-gradient(135deg,#7c6af7,#5a4fd1)!important;
  color:#fff!important;border:none!important;border-radius:8px!important;
  font-weight:700!important;font-family:'Syne',sans-serif!important;
  padding:10px 22px!important;letter-spacing:.02em!important;
  transition:opacity .15s,transform .15s!important;
}
.stButton>button:hover{opacity:.82!important;transform:translateY(-1px)!important;}

/* tabs */
.stTabs [data-baseweb="tab-list"]{
  background:var(--surf)!important;border-radius:10px!important;
  padding:4px!important;border:1px solid var(--border)!important;gap:4px!important;
}
.stTabs [data-baseweb="tab"]{color:var(--muted)!important;font-weight:600!important;border-radius:6px!important;}
.stTabs [aria-selected="true"]{background:var(--accent)!important;color:#fff!important;}

/* cards */
.card{background:var(--surf);border:1px solid var(--border);border-radius:12px;padding:20px 22px;margin-bottom:14px;}
.card:hover{border-color:rgba(124,106,247,.4);}

/* answer */
.answer-box{
  background:linear-gradient(135deg,rgba(124,106,247,.07),rgba(62,207,207,.07));
  border:1px solid rgba(124,106,247,.28);border-radius:12px;
  padding:22px;line-height:1.75;font-size:.91rem;
}
.past-card{background:rgba(62,207,142,.04);border:1px solid rgba(62,207,142,.2);border-radius:8px;padding:11px 14px;margin-bottom:9px;font-size:.82rem;}
.beam-row{background:rgba(247,169,74,.06);border-left:3px solid var(--warn);padding:6px 12px;margin:3px 0;font-size:.76rem;border-radius:0 6px 6px 0;font-family:'Space Mono',monospace;}

/* doc rows */
.doc-row{display:flex;align-items:center;justify-content:space-between;padding:9px 13px;background:var(--surf2);border:1px solid var(--border);border-radius:7px;margin-bottom:5px;font-size:.79rem;}
.doc-row:hover{border-color:rgba(124,106,247,.4);}

/* category pills */
.cat{border-radius:5px;padding:2px 8px;font-size:.67rem;font-weight:700;}
.cat-DOCUMENTS{background:rgba(124,106,247,.15);color:#a78bfa;}
.cat-DECISIONS{background:rgba(62,207,207,.15);color:var(--teal);}
.cat-FINANCIAL{background:rgba(247,169,74,.15);color:var(--warn);}
.cat-OPERATIONS{background:rgba(62,207,142,.15);color:var(--green);}
.cat-PEOPLE{background:rgba(247,106,106,.15);color:var(--red);}
.cat-MEETINGS{background:rgba(99,102,241,.15);color:#818cf8;}

/* chat */
.chat-user{display:flex;justify-content:flex-end;margin-bottom:10px;}
.chat-agent{display:flex;justify-content:flex-start;margin-bottom:10px;}
.bbl-user{background:linear-gradient(135deg,#7c6af7,#5a4fd1);color:#fff;border-radius:18px 18px 4px 18px;padding:11px 16px;max-width:74%;font-size:.88rem;line-height:1.6;}
.bbl-agent{background:var(--surf2);border:1px solid var(--border);color:var(--text);border-radius:18px 18px 18px 4px;padding:11px 16px;max-width:78%;font-size:.88rem;line-height:1.6;}
.bbl-ts{font-size:.61rem;color:var(--muted);margin-top:3px;font-family:'Space Mono',monospace;}
.chat-user .bbl-ts{text-align:right;}.chat-agent .bbl-ts{text-align:left;}
.typing{display:flex;align-items:center;gap:5px;background:var(--surf2);border:1px solid var(--border);border-radius:18px 18px 18px 4px;padding:12px 16px;width:fit-content;}
.dot{width:7px;height:7px;border-radius:50%;background:var(--accent);animation:bounce 1.2s infinite ease-in-out;}
.dot:nth-child(2){animation-delay:.2s;}.dot:nth-child(3){animation-delay:.4s;}

/* stat grid */
.sg{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:12px;}
.sb{background:var(--surf2);border:1px solid var(--border);border-radius:8px;padding:12px;text-align:center;}
.sn{font-size:1.5rem;font-weight:800;line-height:1;font-family:'Space Mono',monospace;}
.sl{font-size:.6rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-top:3px;}

/* connect card (login screen) */
.connect-wrap{
  max-width:480px;margin:60px auto 0;
  background:var(--surf);border:1px solid var(--border);border-radius:16px;padding:40px 44px;
}
.connect-logo{
  width:56px;height:56px;background:linear-gradient(135deg,#7c6af7,#3ecfcf);
  border-radius:14px;display:flex;align-items:center;justify-content:center;
  font-size:26px;margin:0 auto 20px;
}
.connect-title{font-size:1.55rem;font-weight:800;font-family:'Syne',sans-serif;text-align:center;margin-bottom:6px;}
.connect-sub{font-size:.82rem;color:var(--muted);text-align:center;margin-bottom:28px;}
.connect-label{font-size:.78rem;color:var(--muted);font-weight:600;margin-bottom:5px;letter-spacing:.04em;}

/* animations */
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@keyframes slideL{from{opacity:0;transform:translateX(-14px)}to{opacity:1;transform:translateX(0)}}
@keyframes slideR{from{opacity:0;transform:translateX(14px)}to{opacity:1;transform:translateX(0)}}
@keyframes bounce{0%,80%,100%{transform:translateY(0)}40%{transform:translateY(-6px)}}
.fade{animation:fadeIn .35s ease;}
.slideL{animation:slideL .28s ease;}.slideR{animation:slideR .28s ease;}
</style>
""", unsafe_allow_html=True)

# ── session state ──────────────────────────────────────────────────────────────
def _ss(k, v):
    if k not in st.session_state: st.session_state[k] = v

_ss("ready", False)
_ss("dg", None); _ss("hub", None); _ss("dm", None)
_ss("logs", [])
_ss("answer", None); _ss("past_matches", []); _ss("beam_hits", [])
_ss("q_mode", "deep")
_ss("chat_msgs", []); _ss("pending_q", None); _ss("ended_summary", None)
_ss("graph_html", None)
_ss("conn_error", "")

def log(msg):
    st.session_state.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ── auto-connect from saved credentials ───────────────────────────────────────
if not st.session_state.ready:
    _saved = _load_creds()
    if _saved and _saved.get("api_key"):
        # silently attempt reconnect — user won't see the form at all
        _ok, _err = None, None
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            _ak = _saved["api_key"]
            _bu = _saved.get("base_url", "")
            _md = _saved.get("model", "claude-sonnet-4-5")
            if _ak:  os.environ["LLM_API_KEY"]  = _ak
            if _bu:  os.environ["LLM_BASE_URL"] = _bu
            if _md:  os.environ["LLM_MODEL"]    = _md
            import decisiongraph.config as cfg
            from decisiongraph import DecisionGraph, EnterpriseHub, DiscussionManager
            if _ak:  cfg.LLM_API_KEY  = _ak
            if _bu:  cfg.LLM_BASE_URL = _bu
            if _md:  cfg.LLM_MODEL    = _md
            st.session_state.dg  = DecisionGraph()
            st.session_state.hub = EnterpriseHub()
            st.session_state.dm  = DiscussionManager()
            st.session_state.ready = True
            _s = st.session_state.dg.stats()
            st.session_state.logs.append(f"[{time.strftime('%H:%M:%S')}] Auto-connected — {_s['nodes']} nodes")
        except Exception as _e:
            # credentials might be stale/wrong — fall through to manual form
            st.session_state.ready = False
            st.session_state.conn_error = f"Auto-connect failed: {_e}"


# ── connect ────────────────────────────────────────────────────────────────────
def do_connect(api_key, base_url, model):
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        if api_key:  os.environ["LLM_API_KEY"]  = api_key
        if base_url: os.environ["LLM_BASE_URL"] = base_url
        if model:    os.environ["LLM_MODEL"]     = model

        import decisiongraph.config as cfg
        from decisiongraph import DecisionGraph, EnterpriseHub, DiscussionManager

        if api_key:  cfg.LLM_API_KEY  = api_key
        if base_url: cfg.LLM_BASE_URL = base_url
        if model:    cfg.LLM_MODEL    = model

        st.session_state.dg  = DecisionGraph()
        st.session_state.hub = EnterpriseHub()
        st.session_state.dm  = DiscussionManager()
        st.session_state.ready = True
        s = st.session_state.dg.stats()
        log(f"Connected — {s['nodes']} nodes · {s['decisions']} decisions")
        # persist so next restart auto-connects
        _save_creds(api_key, base_url, model)
        return True, ""
    except Exception as e:
        st.session_state.ready = False
        return False, str(e)


# ══════════════════════════════════════════════════════════════════════════════
# NOT CONNECTED  →  show centered connection form, nothing else
# ══════════════════════════════════════════════════════════════════════════════
if not st.session_state.ready:
    st.markdown('<div class="connect-wrap fade">', unsafe_allow_html=True)
    st.markdown("""
    <div class="connect-logo">🧠</div>
    <div class="connect-title">DecisionGraph</div>
    <div class="connect-sub">Enterprise Institutional Memory OS<br>Enter your LLM credentials to get started</div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="connect-label">API KEY</div>', unsafe_allow_html=True)
    api_key = st.text_input("api_key", placeholder="sk-ant-...",
                             type="password", label_visibility="collapsed", key="in_key")

    st.markdown('<div class="connect-label" style="margin-top:12px;">BASE URL <span style="color:#6b6b80;font-weight:400;">(optional — leave blank for Anthropic default)</span></div>', unsafe_allow_html=True)
    base_url = st.text_input("base_url", placeholder="https://api.anthropic.com",
                              label_visibility="collapsed", key="in_url")

    st.markdown('<div class="connect-label" style="margin-top:12px;">MODEL</div>', unsafe_allow_html=True)
    model = st.text_input("model", value="claude-sonnet-4-5",
                           label_visibility="collapsed", key="in_model")

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    if st.button("Connect →", use_container_width=True, key="btn_conn"):
        if not api_key.strip():
            st.error("API key is required.")
        else:
            with st.spinner("Initialising — loading graph and embedding model…"):
                ok, err = do_connect(api_key.strip(), base_url.strip(), model.strip())
            if ok:
                st.rerun()
            else:
                st.error(f"Connection failed: {err}")

    st.markdown('</div>', unsafe_allow_html=True)
    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# CONNECTED — full app
# ══════════════════════════════════════════════════════════════════════════════
dg_ref  = st.session_state.dg
hub_ref = st.session_state.hub
dm_ref  = st.session_state.dm

# ── sidebar (stats + log) ──────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div style="padding:14px 0 6px;">
      <span style="font-size:1.2rem;font-weight:800;font-family:'Syne',sans-serif;">🧠 DecisionGraph</span>
    </div>
    """, unsafe_allow_html=True)
    st.divider()

    s = dg_ref.stats()
    st.markdown("**Stats**")
    st.markdown(f"""
    <div class="sg">
      <div class="sb"><div class="sn" style="color:#7c6af7;">{s['nodes']}</div><div class="sl">nodes</div></div>
      <div class="sb"><div class="sn" style="color:#3ecfcf;">{s['edges']}</div><div class="sl">edges</div></div>
      <div class="sb"><div class="sn" style="color:#f7a94a;">{s['communities']}</div><div class="sl">communities</div></div>
      <div class="sb"><div class="sn" style="color:#3ecf8e;">{s['decisions']}</div><div class="sl">decisions</div></div>
    </div>
    """, unsafe_allow_html=True)

    n_co  = len(hub_ref.companies)
    n_ses = len(dm_ref.list_sessions())
    st.markdown(f"""
    <div class="sg">
      <div class="sb"><div class="sn" style="color:#818cf8;">{n_co}</div><div class="sl">companies</div></div>
      <div class="sb"><div class="sn" style="color:#f472b6;">{n_ses}</div><div class="sl">sessions</div></div>
    </div>
    """, unsafe_allow_html=True)

    st.divider()

    # reconnect / change credentials
    with st.expander("⚙️ Change credentials"):
        r_key   = st.text_input("API Key",   type="password", key="r_key")
        r_url   = st.text_input("Base URL",  key="r_url")
        r_model = st.text_input("Model",     value=os.getenv("LLM_MODEL",""), key="r_model")
        _rc1, _rc2 = st.columns(2)
        with _rc1:
            if st.button("Reconnect", key="btn_reconnect", use_container_width=True):
                with st.spinner("Reconnecting…"):
                    ok, err = do_connect(r_key.strip(), r_url.strip(), r_model.strip())
                if ok: st.rerun()
                else:  st.error(err)
        with _rc2:
            if st.button("Forget", key="btn_forget", use_container_width=True):
                try: _CREDS_FILE.unlink()
                except Exception: pass
                st.session_state.ready = False
                st.rerun()

    st.divider()
    st.markdown("**Log**")
    for entry in st.session_state.logs[-10:]:
        st.markdown(
            f"<div style='font-size:.65rem;color:#6b6b80;font-family:Space Mono;padding:2px 0;border-bottom:1px solid #1e1e2e;'>{entry}</div>",
            unsafe_allow_html=True
        )


# ── page header ────────────────────────────────────────────────────────────────
st.markdown("""
<div style="display:flex;align-items:center;gap:14px;padding:16px 0 22px;border-bottom:1px solid #252535;margin-bottom:22px;" class="fade">
  <div style="width:40px;height:40px;background:linear-gradient(135deg,#7c6af7,#3ecfcf);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0;">🧠</div>
  <div>
    <div style="font-size:1.5rem;font-weight:800;font-family:'Syne',sans-serif;line-height:1.1;">DecisionGraph</div>
    <div style="font-size:.77rem;color:#6b6b80;">Enterprise Memory OS &nbsp;·&nbsp; Knowledge Graph + Decision Memory</div>
  </div>
</div>
""", unsafe_allow_html=True)


# ── ingest helper ──────────────────────────────────────────────────────────────
def ingest_with_progress(files, ingest_fn, label=""):
    import decisiongraph.ingest as imod
    import decisiongraph.graph  as gmod
    ok = 0
    prog   = st.progress(0, text="Starting…")
    status = st.empty()

    for idx, f in enumerate(files):
        ext = os.path.splitext(f.name)[1].lower() or ".txt"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        tmp.write(f.read()); tmp.close()

        n    = len(files)
        base = idx / n
        span = 1 / n
        S = [
            (.05, f"[{idx+1}/{n}] Reading {f.name}…"),
            (.20, f"[{idx+1}/{n}] Chunking text…"),
            (.40, f"[{idx+1}/{n}] Extracting triples (LLM)…"),
            (.55, f"[{idx+1}/{n}] Building graph…"),
            (.65, f"[{idx+1}/{n}] Entity resolution…"),
            (.75, f"[{idx+1}/{n}] Community detection…"),
            (.90, f"[{idx+1}/{n}] Summarising communities…"),
            (1.0, f"[{idx+1}/{n}] Saving…"),
        ]
        def _p(i): prog.progress(base + S[i][0]*span, text=S[i][1])

        orig = {k: getattr(imod if k in("chunk","triple","build") else gmod, {
            "chunk":"chunk_text","triple":"extract_all_triples","build":"build_graph",
            "er":"entity_resolution","comm":"detect_communities",
            "summ":"summarize_communities","save":"save_graph_state"}[k])
            for k in ("chunk","triple","build","er","comm","summ","save")}

        imod.chunk_text            = lambda *a,**kw: (_p(1), orig["chunk"](*a,**kw))[1]
        imod.extract_all_triples   = lambda *a,**kw: (_p(2), orig["triple"](*a,**kw))[1]
        imod.build_graph           = lambda *a,**kw: (_p(3), orig["build"](*a,**kw))[1]
        gmod.entity_resolution     = lambda *a,**kw: (_p(4), orig["er"](*a,**kw))[1]
        gmod.detect_communities    = lambda *a,**kw: (_p(5), orig["comm"](*a,**kw))[1]
        gmod.summarize_communities = lambda *a,**kw: (_p(6), orig["summ"](*a,**kw))[1]
        gmod.save_graph_state      = lambda *a,**kw: (_p(7), orig["save"](*a,**kw))[1]
        _p(0)
        status.markdown(f"<div style='font-size:.81rem;color:#f7a94a;'>Processing: {f.name}</div>", unsafe_allow_html=True)

        try:
            ingest_fn(tmp.name)
            ok += 1
            log(f"Ingested{' ('+label+')' if label else ''}: {f.name}")
        except Exception as e:
            st.error(f"Failed on **{f.name}**: {e}")
            log(f"Error: {f.name}: {e}")
        finally:
            for k,v in orig.items():
                mod = imod if k in("chunk","triple","build") else gmod
                setattr(mod, {"chunk":"chunk_text","triple":"extract_all_triples","build":"build_graph",
                               "er":"entity_resolution","comm":"detect_communities",
                               "summ":"summarize_communities","save":"save_graph_state"}[k], v)
            os.unlink(tmp.name)

    prog.progress(1.0, text="Done!")
    status.empty()
    return ok


# ── TABS ───────────────────────────────────────────────────────────────────────
t1, t2, t3, t4 = st.tabs(["📚 Knowledge Base", "💬 Discussion", "🔍 Query", "🕸️ Graph"])


def stitch_preview(html: str, label: str, height: int = 700):
    """Render a Stitch screen inline, with a toggle to show/hide."""
    if not html:
        return
    key = f"stitch_show_{label}"
    if key not in st.session_state:
        st.session_state[key] = True
    col_a, col_b = st.columns([6, 1])
    with col_a:
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:6px;'>"
            f"<span style='font-size:.65rem;font-weight:700;text-transform:uppercase;"
            f"letter-spacing:.1em;color:#3ecfcf;'>Stitch UI Design</span>"
            f"<span style='font-size:.65rem;color:#6b6b80;'>· live preview from Google Stitch</span></div>",
            unsafe_allow_html=True
        )
    with col_b:
        toggle_lbl = "Hide" if st.session_state[key] else "Show design"
        if st.button(toggle_lbl, key=f"btn_stitch_{label}", use_container_width=True):
            st.session_state[key] = not st.session_state[key]
            st.rerun()
    if st.session_state[key]:
        components.html(html, height=height, scrolling=True)
    st.markdown(
        "<div style='border-top:1px solid #252535;margin:16px 0 20px;"
        "font-size:.72rem;color:#6b6b80;padding-top:8px;'>⬇ Backend Controls</div>",
        unsafe_allow_html=True
    )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — KNOWLEDGE BASE
# ══════════════════════════════════════════════════════════════════════════════
with t1:
    stitch_preview(STITCH_KNOWLEDGE, "kb", height=720)
    L, R = st.columns(2, gap="large")

    # LEFT — personal DG
    with L:
        st.markdown("#### 🔬 Personal Knowledge Graph")
        st.caption("Upload any documents. LLM extracts triples and builds a searchable knowledge graph.")
        dg_files = st.file_uploader(
            "drop_dg", type=["pdf","txt","md","docx","csv","xlsx","xls","eml"],
            accept_multiple_files=True, label_visibility="collapsed", key="up_dg"
        )
        if dg_files:
            for f in dg_files:
                st.markdown(f"<div style='font-size:.75rem;color:#6b6b80;font-family:Space Mono;'>· {f.name}  ({f.size//1024} KB)</div>", unsafe_allow_html=True)

        if st.button("Build Knowledge Graph →", use_container_width=True, key="btn_dg"):
            if not dg_files:
                st.warning("Upload at least one document first.")
            else:
                n = ingest_with_progress(dg_files, dg_ref.ingest)
                if n:
                    s2 = dg_ref.stats()
                    st.success(f"Done — {s2['nodes']} nodes · {s2['edges']} edges · {s2['communities']} communities")
                    st.session_state.graph_html = None
                    st.rerun()

        if dg_ref.G:
            s2 = dg_ref.stats()
            st.markdown(f"""
            <div style="display:flex;gap:8px;margin-top:14px;">
              <div class="sb" style="flex:1;"><div class="sn" style="color:#7c6af7;">{s2['nodes']}</div><div class="sl">nodes</div></div>
              <div class="sb" style="flex:1;"><div class="sn" style="color:#3ecfcf;">{s2['edges']}</div><div class="sl">edges</div></div>
              <div class="sb" style="flex:1;"><div class="sn" style="color:#f7a94a;">{s2['communities']}</div><div class="sl">communities</div></div>
            </div>
            """, unsafe_allow_html=True)

    # RIGHT — CompanyMemory
    with R:
        st.markdown("#### 🏢 Company Memory")
        st.caption("Isolated knowledge per company — multi-tenant, never shared.")

        cos = hub_ref.list_companies()
        co_opts = ["➕ New company"] + [f"{c['name']}  [{c['id']}]" for c in cos]
        sel = st.selectbox("Company", co_opts, key="sel_co")

        if sel == "➕ New company":
            with st.expander("Create new company", expanded=True):
                nid  = st.text_input("Slug ID", placeholder="acme-corp", key="nco_id")
                nnm  = st.text_input("Name",    placeholder="Acme Corp",  key="nco_nm")
                if st.button("Create", key="btn_co_cr"):
                    if nid.strip() and nnm.strip():
                        hub_ref.add_company(nid.strip().lower(), nnm.strip())
                        log(f"Company: {nnm}"); st.rerun()
                    else: st.warning("Fill both fields.")
        else:
            co_id = sel.split("[")[-1].rstrip("]").strip()
            cm    = hub_ref.get_company(co_id)
            if cm:
                cats = cm.stats().get("categories", {})
                if cats:
                    pills = " ".join(f'<span class="cat cat-{k}">{k}&nbsp;{v}</span>' for k,v in cats.items())
                    st.markdown(f"<div style='margin-bottom:8px;'>{pills}</div>", unsafe_allow_html=True)

                cm_files = st.file_uploader(
                    "drop_cm", type=["pdf","txt","md","docx","csv","xlsx","xls","eml"],
                    accept_multiple_files=True, label_visibility="collapsed", key="up_cm"
                )
                if cm_files:
                    from decisiongraph.company import detect_category
                    for f in cm_files:
                        cat = detect_category(f.name)
                        st.markdown(f"<div class='doc-row'><span style='font-family:Space Mono;font-size:.74rem;'>{f.name[:40]}</span><span class='cat cat-{cat}'>{cat}</span></div>", unsafe_allow_html=True)

                if st.button(f"Ingest into {cm.company_name} →", use_container_width=True, key="btn_cm"):
                    if not cm_files: st.warning("Upload files first.")
                    else:
                        n = ingest_with_progress(cm_files, cm.ingest, cm.company_name)
                        if n:
                            cs = cm.stats()
                            st.success(f"Done — {cs['graph']['nodes']} nodes · {cs['documents_ingested']} docs total")
                            st.rerun()

                docs = cm.list_documents()
                if docs:
                    st.markdown(f"<div style='font-size:.75rem;color:#6b6b80;margin:10px 0 5px;'>{len(docs)} documents</div>", unsafe_allow_html=True)
                    for d in docs[:8]:
                        dcat = d["category"]
                        st.markdown(f"<div class='doc-row'><span style='font-family:Space Mono;font-size:.74rem;'>{d['filename'][:36]}</span><span class='cat cat-{dcat}'>{dcat}</span></div>", unsafe_allow_html=True)
                    if len(docs) > 8: st.caption(f"+{len(docs)-8} more")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — DISCUSSION
# ══════════════════════════════════════════════════════════════════════════════
with t2:
    stitch_preview(STITCH_DISCUSSION, "disc", height=700)
    CL, CR = st.columns([1.6, 1], gap="large")

    # RIGHT — controls
    with CR:
        st.markdown("#### Session Controls")
        if dm_ref.active_session:
            sess = dm_ref.active_session
            st.markdown(f"""
            <div style="background:rgba(62,207,142,.07);border:1px solid rgba(62,207,142,.25);border-radius:10px;padding:14px;margin-bottom:12px;">
              <div style="font-size:.65rem;color:#3ecf8e;font-weight:700;text-transform:uppercase;letter-spacing:.08em;">Active session</div>
              <div style="font-weight:700;margin:4px 0;font-size:.95rem;">{sess.title}</div>
              <div style="font-size:.7rem;color:#6b6b80;font-family:'Space Mono',monospace;">{len(sess.messages)} messages · {sess.id}</div>
            </div>
            """, unsafe_allow_html=True)
            if st.button("⏹ End + generate summary", use_container_width=True, key="btn_end"):
                with st.spinner("Generating summary…"):
                    try:
                        ended = dm_ref.end_session(dg_ref.client, memory=dg_ref.memory)
                        st.session_state.ended_summary = dict(
                            title=ended.title, summary=ended.summary,
                            decisions=ended.key_decisions, questions=ended.open_questions,
                            duration=ended.duration_minutes, n=len(ended.messages)
                        )
                        st.session_state.chat_msgs = []
                        log(f"Session ended: {ended.title}"); st.rerun()
                    except Exception as e: st.error(str(e))
        else:
            t_in = st.text_input("Title", placeholder="Q3 product roadmap…", key="s_title")
            co_l = hub_ref.list_companies()
            co_o = ["None"] + [f"{c['name']}  [{c['id']}]" for c in co_l]
            co_s = st.selectbox("Company context", co_o, key="s_co")
            if st.button("▶ Start session", use_container_width=True, key="btn_start"):
                co_id2 = "" if co_s == "None" else co_s.split("[")[-1].rstrip("]").strip()
                dm_ref.start_session(title=t_in.strip(), company_id=co_id2)
                st.session_state.chat_msgs = []
                st.session_state.ended_summary = None
                log(f"Session: {dm_ref.active_session.title}"); st.rerun()

        st.divider()
        st.markdown("**Past sessions**")
        past = dm_ref.list_sessions()
        if not past: st.caption("No sessions yet.")
        for ps in past[:5]:
            with st.expander(f"📋 {ps['title'][:28]}", expanded=False):
                st.markdown(f"<div style='font-size:.72rem;color:#6b6b80;font-family:Space Mono;'>{ps['started_at'][:16]} · {ps['duration_minutes']:.1f}min · {ps['messages_count']} msgs</div><div style='font-size:.79rem;color:#a0a0b8;margin-top:5px;line-height:1.5;'>{(ps.get('summary','') or 'No summary')[:140]}</div>", unsafe_allow_html=True)
                for d in ps.get("key_decisions",[])[:3]:
                    st.markdown(f"<div style='font-size:.74rem;color:#3ecf8e;'>✓ {d}</div>", unsafe_allow_html=True)
                for q in ps.get("open_questions",[])[:2]:
                    st.markdown(f"<div style='font-size:.74rem;color:#f7a94a;'>? {q}</div>", unsafe_allow_html=True)

    # LEFT — chat
    with CL:
        # ended summary card
        if st.session_state.ended_summary and not dm_ref.active_session:
            d = st.session_state.ended_summary
            st.markdown(f"""
            <div style="background:linear-gradient(135deg,rgba(62,207,142,.06),rgba(62,207,207,.06));border:1px solid rgba(62,207,142,.25);border-radius:12px;padding:18px 22px;margin-bottom:16px;" class="fade">
              <div style="font-size:.65rem;color:#3ecf8e;font-weight:700;text-transform:uppercase;letter-spacing:.08em;">Session complete</div>
              <div style="font-size:1rem;font-weight:700;margin:5px 0;">{d['title']}</div>
              <div style="font-size:.82rem;color:#a0a0b8;line-height:1.6;margin-bottom:8px;">{d['summary']}</div>
              <div style="font-size:.7rem;color:#6b6b80;font-family:'Space Mono',monospace;">{d['duration']:.1f} min · {d['n']} messages</div>
            """, unsafe_allow_html=True)
            if d["decisions"]:
                st.markdown("<div style='font-size:.74rem;font-weight:700;color:#3ecf8e;margin:8px 0 4px;'>Key Decisions</div>", unsafe_allow_html=True)
                for dec in d["decisions"]: st.markdown(f"<div style='font-size:.79rem;color:#a0a0b8;margin-bottom:3px;'>✓ {dec}</div>", unsafe_allow_html=True)
            if d["questions"]:
                st.markdown("<div style='font-size:.74rem;font-weight:700;color:#f7a94a;margin:8px 0 4px;'>Open Questions</div>", unsafe_allow_html=True)
                for q in d["questions"]: st.markdown(f"<div style='font-size:.79rem;color:#a0a0b8;margin-bottom:3px;'>? {q}</div>", unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)

        if not dm_ref.active_session:
            if not st.session_state.ended_summary:
                st.markdown("""
                <div style="text-align:center;padding:60px 20px;color:#6b6b80;">
                  <div style="font-size:2.5rem;margin-bottom:10px;">💬</div>
                  <div style="font-weight:700;color:#a0a0b8;margin-bottom:5px;font-size:1rem;">No active session</div>
                  <div style="font-size:.82rem;">Use the controls on the right to start one.</div>
                </div>
                """, unsafe_allow_html=True)
        else:
            # messages
            for msg in st.session_state.chat_msgs:
                if msg["role"] == "user":
                    st.markdown(f"<div class='chat-user slideR'><div><div class='bbl-user'>{msg['content']}</div><div class='bbl-ts'>{msg.get('ts','')}</div></div></div>", unsafe_allow_html=True)
                else:
                    st.markdown(f"<div class='chat-agent slideL'><div><div class='bbl-agent'>{msg['content']}</div><div class='bbl-ts'>{msg.get('ts','')}</div></div></div>", unsafe_allow_html=True)

            # typing indicator (two-render pattern)
            typing_slot = st.empty()
            if st.session_state.pending_q:
                typing_slot.markdown("<div class='chat-agent'><div class='typing'><div class='dot'></div><div class='dot'></div><div class='dot'></div></div></div>", unsafe_allow_html=True)
                q = st.session_state.pending_q
                st.session_state.pending_q = None
                try:
                    co_id3 = dm_ref.active_session.company_id
                    if co_id3 and hub_ref:
                        cm3 = hub_ref.get_company(co_id3)
                        qfn = cm3.query if (cm3 and cm3.dg.G) else dg_ref.query
                    else:
                        qfn = dg_ref.query
                    ans = dm_ref.chat(q, dg_ref.client, qfn, dg_ref.memory, mode="session")
                    st.session_state.chat_msgs.append({"role":"agent","content":ans,"ts":time.strftime("%H:%M")})
                    typing_slot.empty(); log(f"Chat: {q[:40]}…")
                except Exception as e:
                    typing_slot.empty(); st.error(str(e))
                st.rerun()

            # input
            with st.form("chat_form", clear_on_submit=True):
                ci1, ci2 = st.columns([5,1])
                with ci1: user_in = st.text_input("msg", placeholder="Ask anything…", label_visibility="collapsed", key="c_in")
                with ci2: send = st.form_submit_button("Send", use_container_width=True)
            if send and user_in.strip():
                txt = user_in.strip()
                st.session_state.chat_msgs.append({"role":"user","content":txt,"ts":time.strftime("%H:%M")})
                dm_ref.add_message("user", txt)
                st.session_state.pending_q = txt
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — QUERY
# ══════════════════════════════════════════════════════════════════════════════
with t3:
    stitch_preview(STITCH_QUERY, "query", height=700)
    QL, QR = st.columns([1, 1.5], gap="large")

    with QL:
        st.markdown("#### Query Mode")
        mode = st.radio("m", ["normal","session","deep"],
            format_func=lambda x: {"normal":"⚡ Normal — LLM + past decisions","session":"🔗 Session — Graph + decisions","deep":"🧠 Deep — Full ReAct loop"}[x],
            index=["normal","session","deep"].index(st.session_state.q_mode),
            label_visibility="collapsed", key="qmode_r")
        st.session_state.q_mode = mode

        MDESC = {
            "normal":  ("#f7a94a","⚡ Normal","Fast. Past decisions + LLM's own knowledge. No graph traversal."),
            "session": ("#3ecfcf","🔗 Session","Knowledge graph + past decisions in a single LLM call."),
            "deep":    ("#7c6af7","🧠 Deep","Multi-step ReAct loop. Most thorough. Slowest."),
        }
        mc,ml,md = MDESC[mode]
        st.markdown(f"""
        <div style="background:rgba(255,255,255,.03);border:1px solid {mc}30;border-radius:8px;padding:11px 14px;margin:10px 0 16px;">
          <div style="font-weight:700;color:{mc};margin-bottom:3px;">{ml}</div>
          <div style="font-size:.78rem;color:#a0a0b8;line-height:1.5;">{md}</div>
        </div>
        """, unsafe_allow_html=True)

        co_l3 = hub_ref.list_companies()
        sc_opts = ["Personal Graph"] + [c["name"] for c in co_l3]
        scope = st.selectbox("Scope", sc_opts, key="q_sc")

        question = st.text_area("Q", placeholder="What are the key architectural decisions?", height=110, label_visibility="collapsed", key="q_txt")
        qa, qb = st.columns(2)
        with qa: run_q = st.button("Query →", use_container_width=True, key="btn_q")
        with qb:
            if st.button("Clear", use_container_width=True, key="btn_qclr"):
                st.session_state.answer=None; st.session_state.past_matches=[]; st.session_state.beam_hits=[]; st.rerun()

    with QR:
        if run_q and question.strip():
            target = dg_ref; cmeta = None
            if scope != "Personal Graph":
                for c in co_l3:
                    if c["name"] == scope:
                        cm_q = hub_ref.get_company(c["id"])
                        if cm_q and cm_q.dg.G:
                            target = cm_q.dg
                            cmeta  = {"name":cm_q.company_name,"documents_ingested":len(cm_q.registry),"categories":{d["category"]:sum(1 for x in cm_q.registry.values() if x["category"]==d["category"]) for d in cm_q.registry.values()}}
                        break

            if target.G is None and mode != "normal":
                st.warning("No graph yet — using Normal mode."); mode = "normal"

            bh, pc = [], []
            sbox = st.empty()
            with st.spinner(f"{mode} mode running…"):
                try:
                    import decisiongraph.query as qmod
                    ob = qmod.beam_query; omq = target.memory.query
                    sn = [0]
                    def pb(q,G,cs,ci,ce,em,k=None):
                        sn[0]+=1
                        sbox.markdown(f"<div style='font-size:.79rem;color:#f7a94a;font-family:Space Mono;'>Step {sn[0]} — searching graph…</div>",unsafe_allow_html=True)
                        r=ob(q,G,cs,ci,ce,em,k); tr,mt=r
                        for cid in mt: bh.append({"cid":cid,"summary":cs.get(cid,{}).get("summary","")})
                        sbox.markdown(f"<div style='font-size:.79rem;color:#3ecfcf;font-family:Space Mono;'>{len(tr)} facts from {len(mt)} communities</div>",unsafe_allow_html=True)
                        return r
                    def pmq(q,em,top_k=3): r=omq(q,em,top_k); pc.extend(r); return r
                    qmod.beam_query=pb; target.memory.query=pmq

                    if mode=="normal":
                        from decisiongraph.agent import normal_mode
                        ans=normal_mode(question.strip(),target.client,target.memory,target.embed_model)
                    elif mode=="session":
                        from decisiongraph.agent import session_mode
                        ans=session_mode(question.strip(),target.client,target.G,target.summaries,target.community_ids,target.community_embeddings,target.embed_model,target.memory,cmeta)
                    else:
                        from decisiongraph.agent import react_agent
                        ans=react_agent(question.strip(),target.client,target.G,target.summaries,target.community_ids,target.community_embeddings,target.embed_model,target.memory)

                    qmod.beam_query=ob; target.memory.query=omq; sbox.empty()
                    st.session_state.answer=ans; st.session_state.beam_hits=bh; st.session_state.past_matches=pc
                    log(f"Query ({mode}): {question[:40]}…")
                except Exception as e:
                    qmod.beam_query=ob; target.memory.query=omq; sbox.empty()
                    st.error(f"Error: {e}"); log(f"Query error: {e}")

        if st.session_state.answer:
            mc2,ml2,_ = MDESC.get(st.session_state.q_mode,("#7c6af7","?",""))
            st.markdown(f"<span style='background:{mc2}18;border:1px solid {mc2}35;border-radius:20px;padding:3px 11px;font-size:.72rem;font-weight:700;color:{mc2};'>{ml2}</span>", unsafe_allow_html=True)
            st.markdown(f'<div class="answer-box">{st.session_state.answer}</div>', unsafe_allow_html=True)

            if st.session_state.past_matches:
                st.markdown("**Past decisions used**")
                for p in st.session_state.past_matches:
                    st.markdown(f"<div class='past-card'><span style='background:rgba(62,207,142,.15);color:#3ecf8e;border-radius:20px;padding:2px 8px;font-size:.68rem;font-weight:700;font-family:Space Mono;'>sim {p.get('similarity','?')}</span><div style='font-weight:600;margin:5px 0 3px;'>{p['question'][:88]}</div><div style='color:#a0a0b8;font-size:.78rem;'>{p.get('reasoning_summary','')[:100]}</div></div>", unsafe_allow_html=True)

            if st.session_state.beam_hits:
                st.markdown("**Communities traversed**")
                for h in st.session_state.beam_hits:
                    st.markdown(f"<div class='beam-row'>#{h['cid']} · {h['summary'][:68]}{'…' if len(h['summary'])>68 else ''}</div>", unsafe_allow_html=True)
        elif not run_q:
            st.markdown("""
            <div style="text-align:center;padding:70px 20px;">
              <div style="font-size:2.5rem;margin-bottom:10px;">🔍</div>
              <div style="font-weight:700;color:#a0a0b8;margin-bottom:5px;">Ask anything</div>
              <div style="font-size:.81rem;color:#6b6b80;max-width:270px;margin:0 auto;line-height:1.6;">Pick a mode, write your question, hit Query. Every answer is saved as a decision node.</div>
            </div>
            """, unsafe_allow_html=True)

        decs = dg_ref.get_decisions()
        if decs:
            st.divider()
            st.markdown(f"**Decision memory** — {len(decs)} stored")
            for d in reversed(decs[-4:]):
                with st.expander(f"🔵 {d['question'][:68]}", expanded=False):
                    st.markdown(f"<div style='font-size:.82rem;color:#a0a0b8;line-height:1.65;'>{d['answer'][:350]}…</div>", unsafe_allow_html=True)
                    st.markdown(f"<div style='font-size:.7rem;font-family:Space Mono;color:#6b6b80;margin-top:5px;'>ID: {d['id']} · {d['timestamp'][:16]}</div>", unsafe_allow_html=True)
            if st.button("Export JSON", key="btn_exp"):
                st.download_button("Download", data=json.dumps(decs,indent=2), file_name="decisions.json", mime="application/json", key="btn_dl")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — GRAPH
# ══════════════════════════════════════════════════════════════════════════════
with t4:
    stitch_preview(STITCH_GRAPH, "graph", height=720)
    GC, GV = st.columns([1, 3], gap="large")

    with GC:
        st.markdown("#### Graph Controls")
        gtype = st.selectbox("Type", ["Knowledge Graph","Decision Graph","Company Memory","Discussion Sessions"], key="g_type")
        maxn  = st.slider("Max nodes", 30, 500, 200, 10, key="g_maxn")
        if st.button("Render →", use_container_width=True, key="btn_rend"):
            with st.spinner("Building graph…"):
                try:
                    from pyvis.network import Network
                    PAL = ["#7c6af7","#3ecfcf","#f7a94a","#3ecf8e","#f76a6a","#a78bfa","#34d399","#f59e0b","#60a5fa","#f472b6","#10b981","#6366f1","#ec4899","#14b8a6","#f97316"]

                    def mknet(G2, cols, szs, ttls, h="600px"):
                        net = Network(height=h, width="100%", bgcolor="#0a0a0f", font_color="#e8e8f0")
                        net.set_options('{"physics":{"forceAtlas2Based":{"gravitationalConstant":-50,"springLength":200,"springConstant":0.08,"avoidOverlap":0.5},"solver":"forceAtlas2Based","stabilization":{"iterations":150}},"nodes":{"borderWidth":1,"font":{"size":11,"face":"Space Mono"}},"edges":{"smooth":{"type":"continuous"},"width":0.7,"color":{"inherit":false}},"interaction":{"hover":true,"navigationButtons":true}}')
                        for nd in G2.nodes():
                            ns=str(nd)
                            net.add_node(ns, label=ns[:22]+("…" if len(ns)>22 else ""), color=cols.get(nd,"#7c6af7"), size=szs.get(nd,12), title=ttls.get(nd,ns), borderColor="#ffffff18", font={"color":"#e8e8f0"})
                        for u,v in G2.edges():
                            net.add_edge(str(u),str(v),color="#252535",width=0.7)
                        tmp=tempfile.NamedTemporaryFile(delete=False,suffix=".html",mode="w",encoding="utf-8")
                        net.save_graph(tmp.name); tmp.close()
                        html=open(tmp.name,encoding="utf-8").read(); os.unlink(tmp.name)
                        return html

                    html_out = None

                    if gtype == "Knowledge Graph":
                        if not dg_ref.G: st.warning("Ingest documents first.")
                        else:
                            G=dg_ref.G; comms=dg_ref.communities or {}
                            if G.number_of_nodes()>maxn:
                                top=sorted(G.degree(),key=lambda x:x[1],reverse=True)[:maxn]
                                H=G.subgraph([n for n,_ in top])
                            else: H=G
                            n2c={n:cid for cid,nodes in comms.items() for n in nodes}
                            ucids=sorted(set(n2c.get(n,-1) for n in H.nodes()))
                            ccol={c:PAL[i%len(PAL)] for i,c in enumerate(ucids)}
                            deg=dict(H.degree()); mx=max(deg.values()) if deg else 1
                            cols={n:ccol.get(n2c.get(n,-1),"#7c6af7") for n in H.nodes()}
                            szs={n:10+26*(deg.get(n,0)/mx) for n in H.nodes()}
                            ttls={n:f"<b>{n}</b><br>Community {n2c.get(n,'?')}<br>Degree: {deg.get(n,0)}" for n in H.nodes()}
                            html_out=mknet(H,cols,szs,ttls)

                    elif gtype == "Decision Graph":
                        decs=dg_ref.get_decisions()
                        if not decs: st.info("No decisions yet.")
                        else:
                            DG2=nx.DiGraph()
                            for d in decs[:maxn]: DG2.add_node(d["id"])
                            for i,d1 in enumerate(decs[:maxn]):
                                for d2 in decs[i+1:maxn]:
                                    if set(d1.get("communities_used",[]))&set(d2.get("communities_used",[])):
                                        DG2.add_edge(d1["id"],d2["id"])
                            cols={d["id"]:"#7c6af7" for d in decs[:maxn]}
                            szs={d["id"]:18 for d in decs[:maxn]}
                            ttls={d["id"]:f"<b>{d['question'][:60]}</b><br>{d['reasoning_summary'][:80]}" for d in decs[:maxn]}
                            html_out=mknet(DG2,cols,szs,ttls)

                    elif gtype == "Company Memory":
                        cos2=hub_ref.list_companies()
                        if not cos2: st.info("No companies yet.")
                        else:
                            CMG=nx.Graph(); cols,szs,ttls={},{},{}
                            CPL=["#f7a94a","#3ecfcf","#f76a6a","#3ecf8e","#a78bfa","#f472b6"]
                            CCO={"DOCUMENTS":"#7c6af7","DECISIONS":"#3ecfcf","FINANCIAL":"#f7a94a","OPERATIONS":"#3ecf8e","PEOPLE":"#f76a6a","MEETINGS":"#818cf8"}
                            for ci,c in enumerate(cos2):
                                cid2=c["id"]; cm2=hub_ref.get_company(cid2)
                                CMG.add_node(cid2); cols[cid2]=CPL[ci%len(CPL)]; szs[cid2]=28; ttls[cid2]=f"<b>{c['name']}</b><br>{len(cm2.registry)} docs"
                                for did2,doc in list(cm2.registry.items())[:maxn//max(len(cos2),1)]:
                                    s2=did2[:16]; CMG.add_node(s2); CMG.add_edge(cid2,s2)
                                    cols[s2]=CCO.get(doc["category"],"#6b6b80"); szs[s2]=12
                                    ttls[s2]=f"<b>{doc['filename']}</b><br>{doc['category']}"
                            html_out=mknet(CMG,cols,szs,ttls)

                    elif gtype == "Discussion Sessions":
                        sesses=dm_ref.list_sessions()
                        if not sesses: st.info("No sessions yet.")
                        else:
                            DSG=nx.Graph(); cols,szs,ttls={},{},{}
                            for ss in sesses[:maxn]:
                                sid=ss["id"]; DSG.add_node(sid)
                                cols[sid]="#3ecf8e"; szs[sid]=18+min(ss["messages_count"]*2,18)
                                ttls[sid]=f"<b>{ss['title']}</b><br>{ss['messages_count']} msgs · {ss['duration_minutes']:.1f}min"
                                for dec in ss.get("key_decisions",[])[:3]:
                                    did2=f"D:{sid[:6]}:{dec[:10]}"; DSG.add_node(did2); DSG.add_edge(sid,did2)
                                    cols[did2]="#7c6af7"; szs[did2]=11; ttls[did2]=f"<b>Decision</b><br>{dec}"
                                for q2 in ss.get("open_questions",[])[:2]:
                                    qid2=f"Q:{sid[:6]}:{q2[:10]}"; DSG.add_node(qid2); DSG.add_edge(sid,qid2)
                                    cols[qid2]="#f7a94a"; szs[qid2]=9; ttls[qid2]=f"<b>Open Q</b><br>{q2}"
                            html_out=mknet(DSG,cols,szs,ttls)

                    if html_out:
                        st.session_state.graph_html = html_out
                        st.rerun()

                except ImportError:
                    st.error("pyvis not installed. Run: pip install pyvis")
                except Exception as e:
                    st.error(f"Error: {e}")
                    import traceback; st.code(traceback.format_exc())

        # legend
        LGND = {
            "Knowledge Graph":    [("#7c6af7","Community 0"),("#3ecfcf","Community 1"),("#f7a94a","Community 2"),("#3ecf8e","Community 3")],
            "Decision Graph":     [("#7c6af7","Decision node")],
            "Company Memory":     [("#f7a94a","Company hub"),("#7c6af7","Documents"),("#3ecfcf","Decisions"),("#3ecf8e","Operations")],
            "Discussion Sessions":[("#3ecf8e","Session"),("#7c6af7","Key decision"),("#f7a94a","Open question")],
        }
        st.markdown("<div style='margin-top:14px;'>", unsafe_allow_html=True)
        for ch,lb in LGND.get(gtype,[]):
            st.markdown(f"<div style='display:flex;align-items:center;gap:7px;margin-bottom:5px;font-size:.77rem;color:#a0a0b8;'><span style='width:9px;height:9px;border-radius:50%;background:{ch};flex-shrink:0;display:inline-block;'></span>{lb}</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    with GV:
        if st.session_state.graph_html:
            components.html(st.session_state.graph_html, height=640, scrolling=False)
            if dg_ref.G:
                sv = dg_ref.stats()
                st.markdown(f"""
                <div style="display:flex;gap:8px;margin-top:12px;">
                  <div class="sb" style="flex:1;"><div class="sn" style="color:#7c6af7;">{sv['nodes']}</div><div class="sl">nodes</div></div>
                  <div class="sb" style="flex:1;"><div class="sn" style="color:#3ecfcf;">{sv['edges']}</div><div class="sl">edges</div></div>
                  <div class="sb" style="flex:1;"><div class="sn" style="color:#f7a94a;">{sv['communities']}</div><div class="sl">communities</div></div>
                  <div class="sb" style="flex:1;"><div class="sn" style="color:#3ecf8e;">{sv['decisions']}</div><div class="sl">decisions</div></div>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.markdown("""
            <div style="text-align:center;padding:100px 20px;">
              <div style="font-size:3rem;margin-bottom:14px;">🕸️</div>
              <div style="font-weight:700;color:#a0a0b8;font-size:1rem;margin-bottom:7px;">Graph Explorer</div>
              <div style="font-size:.81rem;color:#6b6b80;max-width:280px;margin:0 auto;line-height:1.6;">
                Choose a graph type on the left and click <strong>Render →</strong>
              </div>
            </div>
            """, unsafe_allow_html=True)
