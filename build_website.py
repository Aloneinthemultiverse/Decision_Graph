"""LIVE TEST — OUR OS builds a website from scratch. DG is in the loop.

Nothing here writes app code. This script only hands the kernel a list of
SUBTASK DESCRIPTIONS. For each one the OS does the work:

  DISPATCH  kernel picks an ECC agent (of 63)
  LOAD      kernel slices the DG blueprint into KERNEL-CAG  (Phase 2 CAG)
  GRANT     kernel mints a scoped, audited AgentNet token
  RUN       the REAL executor (llm_executor) asks the LLM — given the DG context
            slice — to WRITE the actual files  (this is the OS's brain, not us)
  LOG/WB    edits → live code graph, decisions → DG memory
  (between subtasks the blueprint grows, so later steps LOAD richer DG context)
  CONSOLIDATE  Phase 4 "sleep": fold all sessions into long-term memory

Then we QUERY DG about the code the OS just wrote, and open the site.

Run:  python build_website.py
"""
from __future__ import annotations
import os, re, sys, shutil, webbrowser, sqlite3
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("LLM_BASE_URL", "http://localhost:8080")
os.environ.setdefault("LLM_API_KEY", "dummy")
MODEL = os.environ.get("BUILD_MODEL", "gemini-3.5-flash-low")

from decisiongraph.core import DecisionGraph
from decisiongraph.kernel import Kernel
from decisiongraph.llm_executor import make_llm_executor
from decisiongraph import code_graph as cg
from decisiongraph import catalog as catalog_mod

ROOT    = os.path.dirname(os.path.abspath(__file__))
SITE    = os.path.join(ROOT, ".tmp", "built_site")
STORAGE = os.path.join(ROOT, ".tmp", "built_storage")
WS      = os.path.join(ROOT, ".tmp", "built_ws")
AGENTS  = os.path.join(ROOT, "ecc", "agents")
REPO    = "demo/recipebox"
BP_PATH = os.path.join(STORAGE, "blueprints",
                       re.sub(r"[^A-Za-z0-9._-]+", "_", REPO.replace("/", "_")) + ".md")
DB      = os.path.join(STORAGE, "code_graph.db")

C = {"cyan":"\033[96m","grn":"\033[92m","yel":"\033[93m","mag":"\033[95m",
     "dim":"\033[2m","bold":"\033[1m"}
def c(s,k): return f"{C[k]}{s}{C['x'] if False else ''}\033[0m"
def hr(): print("\033[2m" + "-"*76 + "\033[0m")

# The product: "RecipeBox" — a personal recipe manager web app.
# We give ONLY descriptions + target paths. The OS writes the code.
SUBTASKS = [
    ("Scaffold the project: a README describing RecipeBox (a web app to save, "
     "categorize and search cooking recipes) and a requirements.txt (flask, pytest).",
     ["README.md", "requirements.txt"]),
    ("Implement the data layer in pure Python: a RecipeStore class with methods "
     "add_recipe(title, category, ingredients, instructions), get_recipe(rid), "
     "list_recipes(), search(query), delete_recipe(rid). In-memory dict store.",
     ["src/store.py"]),
    ("Implement the domain/service layer that calls RecipeStore: functions "
     "create_recipe(...), find_recipes(query), remove_recipe(rid), and "
     "recipe_stats() returning counts per category.",
     ["src/service.py"]),
    ("Implement a small Flask API in src/api.py exposing the service: routes for "
     "POST /recipes, GET /recipes, GET /search, DELETE /recipes/<id>, GET /stats.",
     ["src/api.py"]),
    ("Build the browser UI landing + app: a single working page web/index.html "
     "(plus web/style.css and web/app.js) where the user can add a recipe "
     "(title, category, ingredients, instructions), see the list, search/filter, "
     "and delete — all client-side using localStorage so it works by just opening "
     "the file.",
     ["web/index.html", "web/style.css", "web/app.js"]),
    ("Write pytest tests in tests/test_app.py covering the store and service "
     "layers (add, list, search, delete, stats).",
     ["tests/test_app.py"]),
]


def grow_blueprint(paths):
    os.makedirs(os.path.dirname(BP_PATH), exist_ok=True)
    if not os.path.exists(BP_PATH):
        with open(BP_PATH, "w", encoding="utf-8") as f:
            f.write(f"# Project: {REPO}\n\n## Mission\n\nSnippetVault — a web app to "
                    f"save, tag and search code snippets. Python core in `src/`, "
                    f"browser UI in `web/`.\n\n# Files in {REPO}\n")
    # append a section per file using the ACTUAL content the OS just wrote,
    # so the next subtask LOADs real prior code as context (DG-CAG compounding).
    with open(BP_PATH, "a", encoding="utf-8") as f:
        for p in paths:
            ap = os.path.join(SITE, p)
            folder = os.path.dirname(p) or "."
            snippet = ""
            if os.path.exists(ap) and p.endswith(".py"):
                head = open(ap, encoding="utf-8").read()[:600]
                snippet = f"\nKey definitions:\n```\n{head}\n```\n"
            f.write(f"\n## File: {p}\n\nThe file `{p}` lives in folder `{folder}/`. "
                    f"Part of SnippetVault.{snippet}\n")


_DEF_PREFIXES = ("def ", "async def ", "class ", "function ", "export ", "const ")

def _return_hint(lines, def_idx):
    """Use the CAG-shared code itself to record what a function RETURNS, so the
    next agent agrees on the shape instead of guessing (id vs object vs bool).

    Collect ALL `return` statements in the function body (by indentation), not
    just the first — the first is often a guard clause (e.g. `if not query:
    return list_recipes()`) and reporting only that misleads the next agent into
    thinking it's the function's real result. We list every distinct return so
    the contract reflects the function's true behaviour."""
    try:
        def_indent = len(lines[def_idx]) - len(lines[def_idx].lstrip())
        returns = []
        for j in range(def_idx + 1, min(def_idx + 120, len(lines))):
            ln = lines[j]
            if not ln.strip():
                continue
            indent = len(ln) - len(ln.lstrip())
            if indent <= def_indent:        # dedented back out → left the body
                break
            s = ln.strip()
            if (s.startswith("return ") or s == "return") and s not in returns:
                returns.append(s)
        if not returns:
            return None
        if len(returns) == 1:
            return returns[0]
        return " | ".join(returns)          # multiple paths → show them all
    except Exception:
        pass
    return None

def api_surface(contract=None):
    """FIX #1 — feed the agent EXACT signatures of code DG has already indexed,
    so it imports real names instead of inventing modules. Pulled from the code
    graph symbols table, with the actual def/class line read from disk. We also
    surface each function's first `return` line — the CAG carries the full
    contract (args AND return shape) so later agents don't disagree on it."""
    if not os.path.exists(DB):
        return ""
    try:
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT path, qualified_name, start_line FROM symbols "
            "WHERE repo=? ORDER BY path, start_line", (REPO,)).fetchall()
        conn.close()
    except Exception:
        return ""
    by_file: dict[str, list[str]] = {}
    for path, qn, line in rows:
        ap = os.path.join(SITE, path)
        sig = None
        try:
            if line and os.path.exists(ap):
                file_lines = open(ap, encoding="utf-8").read().splitlines()
                src_line = file_lines[line - 1].strip()
                if src_line.startswith(_DEF_PREFIXES):
                    sig = src_line.rstrip(":").rstrip("{").strip()
                    if src_line.startswith(("def ", "async def ")):
                        rh = _return_hint(file_lines, line - 1)
                        if rh:
                            sig += f"   # returns -> {rh}"
        except Exception:
            pass
        if sig:
            by_file.setdefault(path, []).append(f"    {sig}")
    return "\n".join(f"# {p} — import as `{p[:-3].replace('/', '.')}`\n" + "\n".join(s)
                     for p, s in by_file.items() if s)


def main():
    for d in (SITE, STORAGE, WS):
        shutil.rmtree(d, ignore_errors=True); os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.dirname(BP_PATH), exist_ok=True)
    cg._connect(DB)

    dg = DecisionGraph(storage_dir=STORAGE)
    print("\033[2m  loading embedding model for semantic dispatch…\033[0m")
    k = Kernel(dg, WS, AGENTS, storage_dir=STORAGE, embed_model=dg.embed_model)  # FIX #2

    # PERMANENT-MEMORY CATALOG — the dispatcher reads this menu to pick the right
    # skills + rules for each task (persona still comes from kernel dispatch).
    CATALOG_PATH = os.path.join(ROOT, "ecc", "TASK_CATALOG.md")
    catalog = catalog_mod.load_catalog(CATALOG_PATH)
    print(f"\033[2m  loaded TASK_CATALOG: {sum(len(v) for v in catalog.values())} "
          f"entries across {len(catalog)} sections\033[0m")

    def catalog_advice(contract):
        # CATALOG: skills + file-matched rules chosen from the permanent menu —
        # advisory guidance, kept SEPARATE from the binding code-signature block.
        sel = catalog_mod.select_for_task(catalog, contract.get("task", ""),
                                          contract.get("task_files") or [],
                                          embed_model=dg.embed_model)
        return catalog_mod.render_selection(sel, catalog)

    # extra_context_fn = api_surface (BINDING exact signatures, FIX #1)
    # advice_context_fn = catalog skills/rules (GUIDANCE, kept separate)
    executor = make_llm_executor(dg.client, MODEL, SITE,
                                 extra_context_fn=api_surface,
                                 advice_context_fn=catalog_advice)

    print(f"\n\033[1m  OUR OS IS BUILDING A WEBSITE — model={MODEL}\033[0m")
    print(f"\033[2m  {len(k.agents)} ECC agents · brain={STORAGE}\033[0m\n")

    for i, (task, files) in enumerate(SUBTASKS, 1):
        hr()
        print(f"\033[1m  SUBTASK {i}/{len(SUBTASKS)}:\033[0m {task[:88]}...")
        r = k.run_task(task, task_files=files, repo=REPO, executor=executor,
                       token_budget=4000)
        grow_blueprint(files)

        d, ctx, g = r["dispatch"], r["context"], r["grant"]
        sel = catalog_mod.select_for_task(catalog, task, files, embed_model=dg.embed_model)
        known = len(re.findall(r"^## File:", open(BP_PATH, encoding="utf-8").read(), re.M))
        print(f"   \033[96mDISPATCH\033[0m  → \033[92m{d['agent']}\033[0m \033[2m({d['reason']})\033[0m")
        print(f"   \033[96mCATALOG\033[0m   → skills=\033[92m{sel['skills'] or '[]'}\033[0m "
              f"rules=\033[92m{sel['rules'] or '[]'}\033[0m")
        print(f"   \033[96mLOAD\033[0m      → DG context slice ~{ctx['approx_tokens']} tokens "
              f"(blueprint knows \033[92m{known}\033[0m prior file(s))")
        print(f"   \033[96mGRANT\033[0m     → \033[93m{g['token'][:22]}…\033[0m scope={g['allowed_topics']} write={g['can_write']}")
        st = cg.stats(DB)
        print(f"   \033[95mWRITE-BACK\033[0m→ code graph: \033[92m{st['symbols']} symbols, {st['calls']} calls\033[0m")

    hr(); print("\n\033[1m  INGEST — full structural parse of what the OS built\033[0m\n")
    s = cg.build_from_repo(DB, SITE, REPO)
    print(f"   \033[92m{s['files']}\033[0m files · \033[92m{s['symbols']}\033[0m symbols · \033[92m{s['calls']}\033[0m call edges")

    print("\n\033[1m  QUERY DG about the code IT just wrote\033[0m")
    try:
        topo = cg.analyze_topology(DB, repo=REPO, top_god=3)
        gods = [f"{x.get('leaf')}({x.get('caller_count')})" for x in (topo.get("god_nodes") or [])][:3]
        print(f"   busiest symbols: \033[92m{', '.join(gods) or '(none)'}\033[0m")
        for name in ("add_recipe", "create_recipe", "search"):
            cl = cg.find_callers(DB, name, repo=REPO) or []
            names = [x.get("caller") or x.get("name") or x for x in cl]
            if names:
                print(f"   who calls {name}(): \033[92m{', '.join(map(str, names))}\033[0m")
    except Exception as e:
        print("   (query error:", e, ")")

    hr(); print("\n\033[1m  CONSOLIDATE (sleep)\033[0m\n")
    rep = k.consolidate(run_dream=True)
    hot = ", ".join(f"{h['path']}(x{h['sessions']})" for h in rep["hot_files"][:5])
    print(f"   sessions={rep['sessions_consolidated']} edits={rep['total_edits']} "
          f"decisions={rep['total_decisions']} archived={rep['archived']}")
    print(f"   hot files: \033[92m{hot or '(none)'}\033[0m")

    index = os.path.join(SITE, "web", "index.html")
    hr(); print("\n\033[1m  DONE — the site OUR OS built:\033[0m")
    print(f"   \033[96m{index}\033[0m")
    print(f"   \033[2mrun the backend: cd {SITE} && python -m flask --app src/api run\033[0m\n")
    if os.path.exists(index):
        try: webbrowser.open("file:///" + index.replace("\\", "/"))
        except Exception: pass
    else:
        print("   \033[93m(no web/index.html produced — see executor output above)\033[0m")


if __name__ == "__main__":
    main()
