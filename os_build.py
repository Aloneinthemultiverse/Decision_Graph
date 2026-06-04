"""OS BUILD TEST — give the OS ONE high-level task; the DISPATCHER decomposes it
and chooses every agent/skill/rule; the builder executes each subtask AS the
chosen persona using its skills. Claude only wires + runs + evaluates.

Flow:
  DISPATCH (LLM, ECC master prompt)  task -> subtasks[{section, agents, skills,
                                       rules, files}]  (the brain that chooses)
  then for each subtask:
    GRANT + LOAD (kernel)            scoped token + DG context slice
    BUILD (LLM)                      writes files AS persona + applying skills
    WRITE-BACK                       edits -> code graph, decision -> DG
  CONSOLIDATE                        fold sessions into long-term memory

Usage:  python os_build.py "Build a full-stack ... web app"
"""
from __future__ import annotations
import os, re, sys, shutil, sqlite3, webbrowser
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:8080")  # IPv4 — avoid ::1
os.environ.setdefault("LLM_API_KEY", "dummy")
MODEL = os.environ.get("BUILD_MODEL", "gemini-3.5-flash-low")
# the DISPATCH call is ONE call — use a stronger model here for reliable, bespoke,
# per-feature decomposition (falls back to the build model if not set).
DISPATCH_MODEL = os.environ.get("DISPATCH_MODEL", MODEL)

import httpx, anthropic
from decisiongraph.core import DecisionGraph
from decisiongraph.kernel import Kernel
from decisiongraph.llm_executor import make_llm_executor
from decisiongraph import code_graph as cg
from decisiongraph import catalog as cm
from decisiongraph import dispatcher as dsp
from decisiongraph import scaffold as scaf
from decisiongraph.run_state import RunState
from decisiongraph import dg_sync

DASHBOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
RUN_STATE = os.path.join(DASHBOARD, "run_state.json")

ROOT     = os.path.dirname(os.path.abspath(__file__))
ECC      = os.path.join(ROOT, "ecc")
SITE     = os.path.join(ROOT, ".tmp", "os_site")
# OPTION B: persist into the SAME store the DG app (port 8000) reads. The app gives
# each browser its OWN workspace (storage/workspaces/<token>); we target the MOST
# RECENTLY USED workspace so builds land in the view you actually have open. Falls
# back to the _global company store, or override with DG_STORAGE.
# The DG app reads each browser-workspace's data at  <ws>/personal/  (code_graph.db
# + decision_graph.pkl live there). Point the OS at YOUR workspace's personal dir so
# every build shows up in your DG view. Override anytime with DG_WORKSPACE / DG_STORAGE.
DG_WORKSPACE = os.environ.get("DG_WORKSPACE", "vcoB-xTSn7OJfDLERszlxA")  # YOUR browser workspace
def _active_store():
    if os.environ.get("DG_STORAGE"):
        return os.environ["DG_STORAGE"]
    p = os.path.join(ROOT, "storage", "workspaces", DG_WORKSPACE, "personal")
    if os.path.isdir(os.path.dirname(p)):
        return p
    return os.path.join(ROOT, "storage", "companies", "_global")
STORAGE  = _active_store()
WS       = os.path.join(ROOT, ".tmp", "os_ws")
AGENTS   = os.path.join(ECC, "agents")
CATALOG  = os.path.join(ECC, "TASK_CATALOG.md")
DPROMPT  = os.path.join(ECC, "DISPATCHER_PROMPT.md")
DB       = os.path.join(STORAGE, "code_graph.db")
REPO     = "demo/os_app"   # main() overrides with a UNIQUE per-build repo
BP_PATH  = os.path.join(STORAGE, "blueprints",
                        re.sub(r"[^A-Za-z0-9._-]+", "_", REPO.replace("/", "_")) + ".md")

DEFAULT_TASK = ("Build a full-stack web application: a personal expense tracker "
                "where a user can add expenses (amount, category, note, date), "
                "edit and delete them, search/filter, and view a summary dashboard "
                "with totals per category. Pick a sensible modern stack.")


def hr(): print("\033[2m" + "-" * 78 + "\033[0m")


_EXPORT_RE = (
    r"^\s*(export\s+(default\s+)?(async\s+)?(function|const|let|class|interface|type|enum)\b.*"
    r"|def\s+\w+.*|class\s+\w+.*|module\.exports.*|exports\.\w+.*)")
_DATA_HINTS = ("/data/", "/lib/", "/types/", "schema", "types", "config", "constants", "model")

def _interface_of(path):
    """Language-agnostic public surface of a written file: its export/def lines,
    plus — for small data/schema/type files — a content head so CONSUMING files
    see the REAL exported shape (keys, fields) and don't guess (e.g. .categories)."""
    ap = os.path.join(SITE, path)
    if not os.path.exists(ap):
        return ""
    try:
        text = open(ap, encoding="utf-8").read()
    except Exception:
        return ""
    lines = text.splitlines()
    is_data = any(h in path.lower() for h in _DATA_HINTS)
    # for data/schema/type files (or anything small), show the real shape verbatim
    if is_data or len(lines) <= 40:
        head = "\n".join(lines[:40])
        return f"# {path} — EXACT exported shape (import & match these names/keys):\n```\n{head}\n```"
    exports = [ln.rstrip() for ln in lines if re.match(_EXPORT_RE, ln)]
    if not exports:
        return ""
    body = "\n".join("    " + e.strip().rstrip("{").strip() for e in exports[:30])
    return f"# {path} — exported surface (import & use exactly these):\n{body}"

def api_surface(contract=None):
    """FIX #2 — feed every later subtask the EXACT public surface + data shapes of
    files prior subtasks wrote (all languages), so imports and shapes line up."""
    if not os.path.exists(DB):
        return ""
    try:
        conn = sqlite3.connect(DB)
        paths = [r[0] for r in conn.execute(
            "SELECT DISTINCT path FROM symbols WHERE repo=? ORDER BY path", (REPO,)).fetchall()]
        conn.close()
    except Exception:
        return ""
    # include any prior file on disk too (data files may have 0 indexed symbols)
    seen = set(paths)
    for root, _, files in os.walk(SITE):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), SITE).replace("\\", "/")
            if rel.endswith((".ts", ".tsx", ".js", ".jsx", ".py")) and rel not in seen:
                seen.add(rel); paths.append(rel)
    blocks = [b for b in (_interface_of(p) for p in sorted(seen)) if b]
    return "\n\n".join(blocks)


def grow_blueprint(task_title, files):
    """COMMON CAG — append each subtask's written files (with a content head) into
    the shared blueprint, so the kernel's LOAD step slices REAL prior code into
    KERNEL-CAG for the next subtask. This is the cumulative cross-subtask memory."""
    os.makedirs(os.path.dirname(BP_PATH), exist_ok=True)
    if not os.path.exists(BP_PATH):
        with open(BP_PATH, "w", encoding="utf-8") as f:
            f.write(f"# Project: {REPO}\n\n## Mission\n\nThe website the OS is building.\n\n"
                    f"# Files in {REPO}\n")
    with open(BP_PATH, "a", encoding="utf-8") as f:
        for p in files:
            ap = os.path.join(SITE, p)
            folder = os.path.dirname(p) or "."
            snippet = ""
            if os.path.exists(ap) and p.endswith((".ts", ".tsx", ".js", ".jsx", ".py", ".prisma")):
                head = open(ap, encoding="utf-8").read()[:700]
                snippet = f"\nKey definitions / exported shape:\n```\n{head}\n```\n"
            f.write(f"\n## File: {p}\n\nFrom subtask: {task_title[:80]}. "
                    f"Lives in `{folder}/`.{snippet}\n")


def _norm(p):
    """Normalize a dispatcher file path into the FIXED Next.js structure so every
    run is bootable regardless of what layout the model picked. Coerces stray
    roots (app/, components/, api/, lib/) under src/ consistently."""
    p = p.replace("\\", "/").lstrip("/")
    p = re.sub(r"^(workspace|project|root)/", "", p)
    keep = (p.endswith((".md", ".json", ".prisma", ".config.js", ".config.ts"))
            or p.startswith(("prisma/", "public/", "src/"))
            or "/" not in p)                      # root config files
    if keep:
        return p
    # coerce common stray roots into the canonical src/ layout
    if re.match(r"^app/", p):           return "src/" + p           # app/ -> src/app/
    if re.match(r"^pages/", p):         return "src/app/" + p[len("pages/"):]
    if re.match(r"^api/", p):           return "src/app/" + p       # api/ -> src/app/api/
    if re.match(r"^(components|lib|data|utils|hooks|styles|types|__tests__|tests)/", p):
        return "src/" + p
    if p.endswith((".ts", ".tsx", ".js", ".jsx", ".css")):
        return "src/" + p               # any other source → under src/
    return p

def _w(rel, content):
    p = os.path.join(SITE, rel)
    os.makedirs(os.path.dirname(p) or SITE, exist_ok=True)
    if not os.path.exists(p):                 # never overwrite OS-written code
        open(p, "w", encoding="utf-8").write(content)

def scaffold_stack(all_files):
    """FIX #3 — emit the DETERMINISTIC boot files for the detected stack (the OS
    should never make the model guess boilerplate). Returns the stack name."""
    fl = [f.replace("\\", "/") for f in all_files]
    # robust: any TSX/JSX (or an app/ route) means a Next.js app — paths are
    # already normalized under src/ by _norm, so detection is reliable now.
    is_next = any(f.endswith((".tsx", ".jsx")) for f in fl) or any("app/api/" in f for f in fl)
    if not is_next:
        return None
    _w("package.json", '{\n  "name": "os-app", "version": "0.1.0", "private": true,\n'
       '  "scripts": { "dev": "next dev", "build": "next build", "start": "next start" },\n'
       '  "dependencies": { "next": "14.2.5", "react": "18.3.1", "react-dom": "18.3.1" },\n'
       '  "devDependencies": { "typescript": "5.5.4", "@types/node": "20.14.10",\n'
       '    "@types/react": "18.3.3", "@types/react-dom": "18.3.0",\n'
       '    "tailwindcss": "3.4.7", "postcss": "8.4.40", "autoprefixer": "10.4.19" }\n}\n')
    _w("next.config.js", "/** @type {import('next').NextConfig} */\nmodule.exports = { reactStrictMode: true };\n")
    _w("postcss.config.js", "module.exports = { plugins: { tailwindcss: {}, autoprefixer: {} } };\n")
    _w("tailwind.config.ts", "import type { Config } from 'tailwindcss'\nconst config: Config = "
       "{ content: ['./src/**/*.{js,ts,jsx,tsx,mdx}'], theme: { extend: {} }, plugins: [] }\nexport default config\n")
    _w("tsconfig.json", '{\n  "compilerOptions": { "target": "ES2020", "lib": ["dom","dom.iterable","esnext"],\n'
       '    "allowJs": true, "skipLibCheck": true, "strict": false, "noEmit": true,\n'
       '    "esModuleInterop": true, "module": "esnext", "moduleResolution": "bundler",\n'
       '    "resolveJsonModule": true, "isolatedModules": true, "jsx": "preserve",\n'
       '    "incremental": true, "plugins": [{ "name": "next" }], "paths": { "@/*": ["./src/*"] } },\n'
       '  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],\n'
       '  "exclude": ["node_modules"]\n}\n')
    _w("src/app/globals.css", "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n")
    _w("src/app/layout.tsx", "import './globals.css';\n\nexport default function RootLayout("
       "{ children }: { children: React.ReactNode }) {\n  return (<html lang=\"en\"><body>"
       "{children}</body></html>);\n}\n")
    # HOMEPAGE SAFETY NET — if the dispatcher made components but no page.tsx that
    # mounts them, assemble one deterministically so the site actually renders.
    page = os.path.join(SITE, "src/app/page.tsx")
    comp_dir = os.path.join(SITE, "src/components")
    if not os.path.exists(page) and os.path.isdir(comp_dir):
        comps = []                              # RECURSIVE — catch nested components
        for root, _, files in os.walk(comp_dir):
            for f in sorted(files):
                if not f.endswith(".tsx"):
                    continue
                rel = os.path.relpath(os.path.join(root, f), comp_dir).replace("\\", "/")[:-4]
                ident = re.sub(r"\W", "", os.path.basename(rel))   # safe import name
                comps.append((ident, f"@/components/{rel}"))
        if comps:
            imports = "\n".join(f"import {i} from '{p}';" for i, p in comps)
            body = "\n      ".join(f"<{i} />" for i, _ in comps)
            _w("src/app/page.tsx",
               f"{imports}\n\nexport default function Home() {{\n  return (\n"
               f"    <main>\n      {body}\n    </main>\n  );\n}}\n")
    return "next.js"


_TS_ERR = re.compile(r"^(?P<file>[^()]+?)\((?P<line>\d+),\d+\):\s*(?P<msg>error TS.*)$")

def _npm_install():
    import subprocess
    subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=SITE,
                   capture_output=True, text=True, timeout=400, shell=(os.name == "nt"))

def type_check():
    """Run tsc and return {file_path: [error lines]} (empty dict = clean)."""
    import subprocess
    r = subprocess.run(["npx", "tsc", "--noEmit"], cwd=SITE,
                       capture_output=True, text=True, timeout=200, shell=(os.name == "nt"))
    by_file = {}
    for ln in (r.stdout + r.stderr).splitlines():
        m = _TS_ERR.match(ln.strip())
        if m:
            by_file.setdefault(m.group("file").replace("\\", "/").strip(), []).append(
                ln.strip())
    return by_file


_IMPORT_RE = re.compile(r"""(?:import|export)\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)""")
_KNOWN_DEP_VER = {  # pin common ones; everything else gets "*"
    "vitest": "^2.0.5", "@testing-library/user-event": "^14.5.2",
    "@testing-library/jest-dom": "^6.4.8", "zod": "^3.23.8",
    "clsx": "^2.1.1", "tailwind-merge": "^2.5.2", "date-fns": "^3.6.0",
    "lucide-react": "^0.424.0", "@prisma/client": "^5.18.0", "prisma": "^5.18.0",
    "zustand": "^4.5.4", "swr": "^2.2.5", "axios": "^1.7.3",
}
def auto_deps():
    """Scan every source import; add any missing npm package to package.json and
    reinstall. Kills 'Cannot find module' errors deterministically — you can't fix a
    missing dependency by editing the file, so the OS installs it."""
    import json as _j, subprocess
    pkg_path = os.path.join(SITE, "package.json")
    if not os.path.exists(pkg_path):
        return []
    pkg = _j.load(open(pkg_path, encoding="utf-8"))
    have = set(pkg.get("dependencies", {})) | set(pkg.get("devDependencies", {}))
    have |= {"react", "react-dom", "next"}
    found = set()
    for root, _, files in os.walk(os.path.join(SITE, "src")):
        for fn in files:
            if not fn.endswith((".ts", ".tsx", ".js", ".jsx")): continue
            try: txt = open(os.path.join(root, fn), encoding="utf-8").read()
            except Exception: continue
            for m in _IMPORT_RE.finditer(txt):
                spec = m.group(1) or m.group(2) or ""
                if not spec or spec.startswith((".", "/", "@/")): continue   # local
                parts = spec.split("/")
                name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
                if name and name not in have: found.add(name)
    if not found:
        return []
    pkg.setdefault("devDependencies", {})
    for n in sorted(found):
        pkg["devDependencies"][n] = _KNOWN_DEP_VER.get(n, "*")
    _j.dump(pkg, open(pkg_path, "w", encoding="utf-8"), indent=2)
    subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=SITE,
                   capture_output=True, text=True, timeout=400, shell=(os.name == "nt"))
    return sorted(found)


def self_correcting_boot_gate(stack, client, agents_by_name, max_rounds=3, rs=None):
    """Compile → auto-install missing deps → if errors, dispatch build-error-resolver
    to FIX erroring files → recompile. Loop until green or exhausted."""
    if stack != "nextjs":
        if rs: rs.boot("skipped")
        return
    from decisiongraph.llm_executor import messages_with_retry, _FILE_RE, _SYS
    print("\n\033[1m  BOOT GATE — npm install + auto-deps + self-correcting type-check\033[0m")
    try:
        _npm_install()
        added=auto_deps()
        if added: print(f"   \033[96mAUTO-DEPS\033[0m installed missing: \033[92m{', '.join(added)}\033[0m")
    except Exception as e:
        print(f"   npm install failed: {e}"); return

    resolver = (agents_by_name.get("build-error-resolver")
                or agents_by_name.get("typescript-reviewer") or {})
    persona = (resolver.get("body") or "").strip()[:1500]

    prev_total = None
    last_snapshot = {}      # path -> content BEFORE this round's fixes (for revert)
    for rnd in range(1, max_rounds + 1):
        errs = type_check()
        nerr = sum(len(v) for v in errs.values())
        if not errs:
            print(f"   \033[92mtype-check PASSED (round {rnd}) — the OS's code compiles\033[0m")
            if rs: rs.boot("pass", 0)
            return
        # MONOTONIC GUARD: if the previous round's fixes made things WORSE, revert
        # them — never keep a regressive fix.
        if prev_total is not None and nerr > prev_total:
            for p, c in last_snapshot.items():
                open(os.path.join(SITE, p), "w", encoding="utf-8").write(c)
            print(f"   \033[93mround {rnd}: {nerr} > {prev_total} — fixes regressed, "
                  f"reverted; keeping best ({prev_total} errors)\033[0m")
            if rs: rs.add_round(rnd, nerr, [], reverted=True); rs.boot("fail", prev_total)
            return
        print(f"   \033[93mround {rnd}: {nerr} error(s) across {len(errs)} file(s)\033[0m")
        if rnd == max_rounds:
            for f, v in list(errs.items())[:4]:
                print(f"     {f}: {v[0][:110]}")
            print(f"   \033[93m(still {nerr} errors after {max_rounds} fix rounds)\033[0m")
            if rs: rs.boot("fail", nerr, [f for f in errs])
            return
        if rs: rs.add_round(rnd, nerr, list(errs.keys()))
        prev_total = nerr
        last_snapshot = {}
        # dispatch build-error-resolver to fix each erroring file
        for fpath, msgs in errs.items():
            ap = os.path.join(SITE, fpath)
            if not os.path.exists(ap):
                continue
            cur = open(ap, encoding="utf-8").read()
            last_snapshot[fpath] = cur          # remember pre-fix version
            print(f"   \033[96mFIX\033[0m build-error-resolver → {fpath} ({len(msgs)} err)")
            surface = api_surface()  # exported shapes/types of ALL files, for cross-file type fixes
            prompt = (
                f"# YOU ARE THE `build-error-resolver` AGENT.\n{persona}\n\n"
                f"The file `{fpath}` has these TypeScript/build errors:\n"
                + "\n".join(msgs[:12]) +
                (f"\n\n# Exact types/shapes from OTHER files (align to these, don't guess):\n{surface}\n" if surface else "") +
                f"\n\nHere is the CURRENT file:\n```\n{cur}\n```\n\n"
                f"Output the COMPLETE corrected `{fpath}` (fixing ALL the errors, "
                f"keeping behavior) using exactly:\n"
                f"===FILE: {fpath}===\n<full corrected file>\n===END===")
            try:
                msg = messages_with_retry(client, model=MODEL, max_tokens=8000,
                    system=_SYS, messages=[{"role": "user", "content": prompt}])
                text = "".join(b.text for b in getattr(msg, "content", [])
                               if getattr(b, "type", None) == "text")
                m = _FILE_RE.search(text)
                body = (m.group("body") if m else text)
                body = re.sub(r"(?m)^\s*===\s*(FILE|END)\b.*$", "", body)
                body = re.sub(r"(?m)^\s*```[a-zA-Z0-9]*\s*$", "", body).strip("\n")
                if body.strip() and body.count("{") == body.count("}"):
                    open(ap, "w", encoding="utf-8").write(
                        body if body.endswith("\n") else body + "\n")
            except Exception as e:
                print(f"     fix failed: {type(e).__name__}")


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK
    # OPTION B: unique repo per build so builds ACCUMULATE in the shared DG store
    global REPO, BP_PATH
    import time as _t
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:32].strip("-") or "app"
    REPO = f"build/{slug}-{int(_t.time()) % 100000}"
    BP_PATH = os.path.join(STORAGE, "blueprints",
                           re.sub(r"[^A-Za-z0-9._-]+", "_", REPO.replace("/", "_")) + ".md")
    for d in (SITE, WS):                  # wipe build artifacts only — KEEP the DG store
        shutil.rmtree(d, ignore_errors=True); os.makedirs(d, exist_ok=True)
    os.makedirs(STORAGE, exist_ok=True)  # persistent DG (port-8000 store) — never wiped
    os.makedirs(os.path.dirname(BP_PATH), exist_ok=True)   # COMMON CAG blueprint dir
    cg._connect(DB)

    dg = DecisionGraph(storage_dir=STORAGE)
    k = Kernel(dg, WS, AGENTS, storage_dir=STORAGE, embed_model=dg.embed_model)

    # The gateway drops keep-alive connections, so the SDK's pooled connection
    # fails on reuse. Use a FRESH connection per request (Connection: close, no
    # keepalive) — exactly how curl behaves, which works. This is the real fix
    # for the "connect then disconnect" APIConnectionError loop.
    http_client = httpx.Client(
        headers={"Connection": "close"},
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=4),
        timeout=httpx.Timeout(150.0),
    )
    client = anthropic.Anthropic(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ.get("LLM_API_KEY", "dummy"),
        http_client=http_client, max_retries=0,
    )
    agents_by_name = {a["name"]: a for a in k.agents}
    catalog = cm.load_catalog(CATALOG)

    hr(); print(f"\033[1m  OS BUILD — model={MODEL}\033[0m")
    print(f"\033[2m  task: {task[:96]}…\033[0m\n")

    # ── DISPATCH: the LLM brain decomposes + chooses everything ──────────────
    print("\033[1m  DISPATCHER (ECC master prompt) is decomposing the task…\033[0m")
    plan = dsp.dispatch_task(client, DISPATCH_MODEL, DPROMPT, task, max_tokens=6000)
    subs = plan.get("subtasks") or []
    if not subs:
        print("\033[93m  dispatcher produced no subtasks — aborting.\033[0m")
        print(plan.get("raw", "")[:1500]); return
    print(f"   task_type=\033[92m{plan['task_type']}\033[0m  "
          f"cross_cutting=\033[92m{plan.get('cross_cutting')}\033[0m\n")

    # LIVE STATE for the dashboard
    rs = RunState(RUN_STATE, task=task, model=MODEL)
    rs.set_plan(plan.get("task_type", ""), plan.get("cross_cutting", []), subs)

    # per-subtask advice closure (persona + skills + rules the dispatcher chose)
    current = {"sub": None}

    def advice(contract):
        sub = current["sub"] or {}
        parts = []
        pb = cm.build_persona_block(agents_by_name, sub.get("agents") or [])
        if pb:
            parts.append(pb)
        sb = cm.build_skills_block(ECC, sub.get("skills") or [])
        if sb:
            parts.append(sb)
        if sub.get("rules"):
            parts.append("# Follow these rule sets:\n- " + "\n- ".join(sub["rules"]))
        if plan.get("cross_cutting"):
            parts.append("# Cross-cutting concerns active on every subtask:\n- "
                         + "\n- ".join(plan["cross_cutting"]))
        return "\n\n".join(parts)

    executor = make_llm_executor(client, MODEL, SITE,
                                 extra_context_fn=api_surface,
                                 advice_context_fn=advice)

    # ── ZOOM IN: build each subtask as the chosen persona + skills ───────────
    for i, sub in enumerate(subs, 1):
        current["sub"] = sub
        files = [_norm(f) for f in (sub.get("files") or [])]
        sub["files"] = files                     # keep normalized for scaffold/ingest
        agents = sub.get("agents") or []
        force = next((a for a in agents if a in agents_by_name), None)
        hr()
        print(f"\033[1m  SUBTASK {i}/{len(subs)} [{sub.get('section','?')}] "
              f"{sub.get('step','')}\033[0m  {sub.get('title','')[:60]}")
        print(f"   \033[96mAGENTS\033[0m  dispatcher chose: \033[92m{agents}\033[0m "
              f"→ builder = \033[92m{force or '(heuristic)'}\033[0m")
        print(f"   \033[96mSKILLS\033[0m  \033[92m{sub.get('skills') or '[]'}\033[0m")
        print(f"   \033[96mRULES\033[0m   \033[92m{sub.get('rules') or '[]'}\033[0m")
        rs.subtask(i - 1, status="running", builder=force, files=files)
        if not files:
            print("   \033[93m(no files for this subtask — skipping build)\033[0m")
            rs.subtask(i - 1, status="done"); continue
        r = k.run_task(sub.get("description") or sub.get("title") or task,
                       task_files=files, repo=REPO, executor=executor,
                       token_budget=4000, force_agent=force)
        grow_blueprint(sub.get("title") or "", files)   # COMMON CAG grows here
        ctx = r.get("context", {})
        known = 0
        if os.path.exists(BP_PATH):
            known = open(BP_PATH, encoding="utf-8").read().count("## File:")
        st = cg.stats(DB)
        print(f"   \033[96mLOAD\033[0m    CAG slice ~{ctx.get('approx_tokens',0)} tokens "
              f"(blueprint knows \033[92m{known}\033[0m prior file(s))")
        print(f"   \033[95mWRITE-BACK\033[0m code graph: "
              f"\033[92m{st['symbols']} symbols, {st['calls']} calls\033[0m")
        rs.subtask(i - 1, status="done", files=files)
        rs.cag(known); rs.graph(st["symbols"], st["calls"])

    # FIX #3 — OS-owned scaffolding: deterministic per-stack template registry.
    # Only for SOFTWARE tasks — a marketing/research deliverable gets no web scaffold.
    is_code = dsp.is_software_task(plan)
    all_files = [f for sub in subs for f in (sub.get("files") or [])]
    stack = (plan.get("stack") or scaf.detect_stack(all_files)) if is_code else None
    if stack:
        wrote = scaf.scaffold(SITE, stack)
        hp = scaf.ensure_homepage(SITE)
        print(f"\n   \033[96mSCAFFOLD\033[0m  OS laid down {len(wrote)} boot file(s) for "
              f"stack=\033[92m{stack}\033[0m" + (f" + assembled {hp}" if hp else ""))
        rs.set(scaffold={"stack": stack, "files": len(wrote)})
    elif not is_code:
        print(f"\n   \033[2m(non-code task — skipping scaffold/boot-gate; "
              f"deliverables are documents)\033[0m")
        rs.set(scaffold={"stack": "documents", "files": 0})

    hr(); print("\n\033[1m  INGEST — full parse of what the OS built\033[0m")
    s = cg.build_from_repo(DB, SITE, REPO)
    print(f"   \033[92m{s['files']}\033[0m files · \033[92m{s['symbols']}\033[0m symbols · "
          f"\033[92m{s['calls']}\033[0m call edges")
    rs.graph(s["symbols"], s["calls"])

    hr(); print("\n\033[1m  CONSOLIDATE (sleep)\033[0m")
    rep = k.consolidate(run_dream=True)
    print(f"   sessions={rep['sessions_consolidated']} edits={rep['total_edits']} "
          f"decisions={rep['total_decisions']} archived={rep['archived']}")
    rs.set(memory={"sessions": rep.get("sessions_consolidated", 0),
                   "edits": rep.get("total_edits", 0),
                   "decisions": rep.get("total_decisions", 0),
                   "archived": rep.get("archived", 0),
                   "hot_files": [h.get("path") for h in (rep.get("hot_files") or [])][:6]})

    if os.environ.get("BOOT_GATE", "1") == "1":
        self_correcting_boot_gate(stack, client, agents_by_name, rs=rs)

    # DG INGEST — feed the built site into DG's KNOWLEDGE GRAPH, exactly like
    # ingesting a GitHub repo (ingest_repo builds nodes+edges the UI draws + lets
    # you ask "how does this project work / why"). THIS is what makes DG the OS's
    # queryable secondary memory of the code it wrote.
    hr(); print("\n\033[1m  DG INGEST — local-folder variant of the GitHub ingest pipeline\033[0m")
    try:
        from decisiongraph.dg_local_ingest import ingest_built_site
        res = ingest_built_site(DG_WORKSPACE, SITE, REPO)
        if res.get("ok"):
            print(f"   \033[92m✓ knowledge graph: {res.get('nodes',0)} nodes · "
                  f"{res.get('edges',0)} edges · {res.get('communities',0)} communities\033[0m")
            print(f"   blueprint: {res.get('blueprint_chars',0)} chars · "
                  f"code_graph: {res.get('code_graph',{})}")
            print(f"   \033[96mview at: http://localhost:8000/?ws={DG_WORKSPACE}\033[0m")
        else:
            print(f"   \033[93mingest issue: {res.get('error') or res.get('ingest_error')}\033[0m")
            if res.get("blueprint_path"):
                print(f"   blueprint saved at {res['blueprint_path']} ({res.get('blueprint_chars',0)} chars)")
    except Exception as e:
        print(f"   \033[93mingest failed: {type(e).__name__}: {e}\033[0m")

    # SHARED DG SYNC — push this build's decisions THROUGH the DG server into the
    # OS workspace, so they show in the DG app's Knowledge / Decision views.
    hr(); print("\n\033[1m  DG SYNC — writing decisions into shared DG (workspace=os-builds)\033[0m")
    s = cg.stats(DB)
    summary = {"files": s.get("files", 0), "symbols": s.get("symbols", 0),
               "calls": s.get("calls", 0), "task_type": plan.get("task_type", ""),
               "boot": (rs.data.get("boot_gate") or {}).get("status", "?"),
               "agents": sorted({(x.get("builder") or (x.get("agents") or ["?"])[0])
                                 for x in subs})}
    res = dg_sync.sync_build(DG_WORKSPACE, task, REPO, subs, summary)
    if res.get("ok"):
        print(f"   \033[92m✓ {res['posted']} decisions pushed to DG\033[0m  "
              f"→ view at \033[96mhttp://localhost:8000/?ws={DG_WORKSPACE}\033[0m")
    else:
        print(f"   \033[93m({res.get('reason','sync failed')} — start server.py to enable)\033[0m")
    rs.done()

    # try to open an entry HTML if one was produced
    for cand in ("index.html", "client/index.html", "public/index.html",
                 "client/src/App.jsx", "web/index.html"):
        p = os.path.join(SITE, cand)
        if os.path.exists(p):
            print(f"\n\033[1m  built entry:\033[0m \033[96m{p}\033[0m")
            if cand.endswith(".html"):
                try: webbrowser.open("file:///" + p.replace("\\", "/"))
                except Exception: pass
            break
    print(f"\n\033[2m  output dir: {SITE}\033[0m")


if __name__ == "__main__":
    main()
