"""DEMO — Build a real website from scratch THROUGH the kernel, firing every phase.

This is the showcase: one command builds a working Todo web app, and every
component is produced by running it through the full Enterprise Agentic OS loop:

    PHASE 3  DISPATCH  pick the right ECC agent (out of 63) for each subtask
             LOAD      slice the growing blueprint into KERNEL-CAG (Phase 2 CAG)
             GRANT     mint a scoped, audited AgentNet token (deny-by-default)
             RUN+LOG   the executor writes REAL files; edits/decisions logged
    DG       WRITE-BACK each edit feeds the live code graph (symbols + calls)
    PHASE 4  CONSOLIDATE the "sleep" pass folds every session into long-term memory

HONESTY: the `executor` here is a scripted stand-in for Claude Code (it writes
predetermined real file content). On the second PC, you swap this one function
for the real Claude Code executor and nothing else changes. Everything ELSE in
this script — dispatch, the CAG slice, the scoped grant, the audit log, the code
graph, the consolidation — is the real system doing real work.

Run:  python demo_build_app.py
"""
from __future__ import annotations
import os, re, shutil, time, json, webbrowser, sys
try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows console: allow box-drawing/arrows
except Exception:
    pass

from decisiongraph.core import DecisionGraph
from decisiongraph.kernel import Kernel
from decisiongraph import code_graph as cg

ROOT     = os.path.dirname(os.path.abspath(__file__))
SITE     = os.path.join(ROOT, ".tmp", "demo_site")          # the website we build
STORAGE  = os.path.join(ROOT, ".tmp", "demo_storage")       # DG brain for this demo
WS       = os.path.join(ROOT, ".tmp", "demo_ws")            # AgentNet workspace
AGENTS   = os.path.join(ROOT, "ecc", "agents")
REPO     = "demo/todo-app"
BP_PATH  = os.path.join(STORAGE, "blueprints", re.sub(r"[^A-Za-z0-9._-]+", "_", REPO.replace("/", "_")) + ".md")
DB       = os.path.join(STORAGE, "code_graph.db")

C = {"cyan": "\033[96m", "grn": "\033[92m", "yel": "\033[93m", "mag": "\033[95m",
     "dim": "\033[2m", "bold": "\033[1m", "red": "\033[91m", "x": "\033[0m"}
def c(s, col): return f"{C[col]}{s}{C['x']}"
def hr(): print(c("─" * 74, "dim"))


# ── the website, decomposed into subtasks (what a PM would hand the kernel) ──
# each: (task prompt, [target files], {path: real file content})
SUBTASKS = [
    ("scaffold the project: add a README and python requirements",
     ["README.md", "requirements.txt"], {
        "README.md": "# Todo App\n\nA tiny todo web app. `src/` is the Python core, "
                     "`web/` is the browser UI.\n\n## Run\nOpen `web/index.html`.\n",
        "requirements.txt": "flask>=3.0\npytest>=8.0\n",
    }),
    ("implement the data layer: a TaskStore with add, complete, list, delete",
     ["src/todo_store.py"], {
        "src/todo_store.py":
            '"""In-memory task store — the data layer."""\n\n'
            "class TaskStore:\n"
            "    def __init__(self):\n"
            "        self._tasks = {}\n"
            "        self._next = 1\n\n"
            "    def add_task(self, title):\n"
            "        tid = self._next\n"
            "        self._tasks[tid] = {'id': tid, 'title': title, 'done': False}\n"
            "        self._next += 1\n"
            "        return self._tasks[tid]\n\n"
            "    def complete_task(self, tid):\n"
            "        if tid in self._tasks:\n"
            "            self._tasks[tid]['done'] = True\n"
            "        return self._tasks.get(tid)\n\n"
            "    def list_tasks(self):\n"
            "        return list(self._tasks.values())\n\n"
            "    def delete_task(self, tid):\n"
            "        return self._tasks.pop(tid, None)\n",
    }),
    ("build the API layer: REST handlers that call the TaskStore",
     ["src/api.py"], {
        "src/api.py":
            '"""API layer — thin handlers over TaskStore (this is the chokepoint)."""\n'
            "from src.todo_store import TaskStore\n\n"
            "store = TaskStore()\n\n"
            "def create(title):\n"
            "    return store.add_task(title)\n\n"
            "def finish(tid):\n"
            "    return store.complete_task(tid)\n\n"
            "def all_tasks():\n"
            "    return store.list_tasks()\n\n"
            "def remove(tid):\n"
            "    return store.delete_task(tid)\n",
    }),
    ("create the browser UI: a working todo page with vanilla JS + localStorage",
     ["web/index.html", "web/app.js", "web/style.css"], {
        "web/index.html":
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Todo</title><link rel=stylesheet href=style.css></head><body>"
            "<main><h1>todo</h1>"
            "<form id=f><input id=t placeholder='what needs doing?' autocomplete=off>"
            "<button>add</button></form><ul id=list></ul></main>"
            "<script src=app.js></script></body></html>\n",
        "web/style.css":
            "*{box-sizing:border-box;font-family:system-ui,sans-serif}"
            "body{background:#0f1220;color:#e8e8f0;display:grid;place-items:center;"
            "min-height:100vh;margin:0}main{width:360px}h1{font-weight:800;letter-spacing:-1px}"
            "form{display:flex;gap:8px}input{flex:1;padding:10px;border-radius:8px;border:0;"
            "background:#1c2138;color:#fff}button{padding:10px 16px;border:0;border-radius:8px;"
            "background:#6c5ce7;color:#fff;font-weight:700;cursor:pointer}"
            "ul{list-style:none;padding:0}li{display:flex;justify-content:space-between;"
            "padding:10px;margin-top:8px;background:#1c2138;border-radius:8px;cursor:pointer}"
            "li.done span{text-decoration:line-through;opacity:.5}li b{color:#ff6b6b;cursor:pointer}\n",
        "web/app.js":
            "const KEY='todo.v1';const $=s=>document.querySelector(s);"
            "let tasks=JSON.parse(localStorage.getItem(KEY)||'[]');"
            "function save(){localStorage.setItem(KEY,JSON.stringify(tasks));render();}"
            "function render(){const l=$('#list');l.innerHTML='';"
            "tasks.forEach((t,i)=>{const li=document.createElement('li');"
            "if(t.done)li.className='done';li.innerHTML=`<span>${t.title}</span><b>x</b>`;"
            "li.querySelector('span').onclick=()=>{t.done=!t.done;save();};"
            "li.querySelector('b').onclick=e=>{e.stopPropagation();tasks.splice(i,1);save();};"
            "l.appendChild(li);});}"
            "$('#f').onsubmit=e=>{e.preventDefault();const v=$('#t').value.trim();"
            "if(v){tasks.push({title:v,done:false});$('#t').value='';save();}};render();\n",
    }),
    ("write tests for the data + api layers",
     ["tests/test_app.py"], {
        "tests/test_app.py":
            "from src.todo_store import TaskStore\n"
            "from src import api\n\n"
            "def test_add_and_list():\n"
            "    s = TaskStore(); s.add_task('a'); s.add_task('b')\n"
            "    assert len(s.list_tasks()) == 2\n\n"
            "def test_complete():\n"
            "    s = TaskStore(); t = s.add_task('x'); s.complete_task(t['id'])\n"
            "    assert s.list_tasks()[0]['done'] is True\n\n"
            "def test_api_create_calls_store():\n"
            "    before = len(api.all_tasks()); api.create('via api')\n"
            "    assert len(api.all_tasks()) == before + 1\n",
    }),
]


def make_executor(files_for_task):
    """Scripted stand-in for Claude Code. Writes the REAL files to disk and
    returns the structured result the kernel logs + writes back to DG."""
    def executor(contract):
        edits, tool_calls = [], []
        for path, text in files_for_task.items():
            abs_path = os.path.join(SITE, path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(text)
            tool_calls.append({"tool": "Write", "path": path, "bytes": len(text)})
            edits.append({"path": path, "summary": f"create {path}", "text": text})
        return {
            "tool_calls": tool_calls,
            "edits": edits,
            "decisions": [{
                "question": f"How to implement: {contract['task'][:60]}?",
                "answer": f"Created {', '.join(files_for_task)} per the {contract['agent']} agent.",
                "reasoning": "Scripted demo executor (stands in for Claude Code).",
            }],
            "notes": [f"agent={contract['agent']} files={list(files_for_task)}"],
        }
    return executor


def append_to_blueprint(files_for_task):
    """Grow the blueprint (DG-CAG) so the NEXT subtask's LOAD pulls in the
    already-built sibling files as context. This is the CAG compounding."""
    os.makedirs(os.path.dirname(BP_PATH), exist_ok=True)
    if not os.path.exists(BP_PATH):
        with open(BP_PATH, "w", encoding="utf-8") as f:
            f.write(f"# Project: {REPO}\n\n## Mission\n\nA tiny todo web app: "
                    f"a Python core (`src/`) and a browser UI (`web/`).\n\n"
                    f"# Files in {REPO}\n")
    with open(BP_PATH, "a", encoding="utf-8") as f:
        for path in files_for_task:
            folder = os.path.dirname(path) or "."
            f.write(f"\n## File: {path}\n\nThe file `{path}` lives in folder "
                    f"`{folder}/`. Part of the todo app.\n")


def main():
    # clean slate
    for d in (SITE, STORAGE, WS):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.dirname(BP_PATH), exist_ok=True)
    cg._connect(DB)  # create an empty code graph so write-back can populate it

    dg = DecisionGraph(storage_dir=STORAGE)
    k = Kernel(dg, WS, AGENTS, storage_dir=STORAGE)

    print(c("\n  BUILDING A WEBSITE FROM SCRATCH — THROUGH THE KERNEL\n", "bold"))
    print(c(f"  {len(k.agents)} ECC agents available · brain={STORAGE}\n", "dim"))

    for i, (task, target_files, contents) in enumerate(SUBTASKS, 1):
        hr()
        print(c(f"  SUBTASK {i}/{len(SUBTASKS)}: ", "bold") + task)
        r = k.run_task(task, task_files=target_files, repo=REPO,
                       executor=make_executor(contents))
        append_to_blueprint(contents)   # grow DG-CAG so the NEXT subtask loads it

        d = r["dispatch"]
        print(f"   {c('DISPATCH', 'cyan')}  → agent " + c(d["agent"], "grn")
              + c(f"  ({d['reason']})", "dim"))
        ctx = r["context"]
        known = len(re.findall(r"^## File:", open(BP_PATH, encoding="utf-8").read(), re.M)) if os.path.exists(BP_PATH) else 0
        print(f"   {c('LOAD','cyan')}      → CAG slice: ~{ctx['approx_tokens']} tokens "
              f"(blueprint now knows {c(str(known),'grn')} prior file(s))")
        g = r["grant"]
        print(f"   {c('GRANT','cyan')}     → " + c(g["token"], "yel")
              + f"  scope={g['allowed_topics']}  write={g['can_write']}")
        print(f"   {c('RUN+LOG','cyan')}   → wrote " + c(f"{len(r['result']['edits'])} file(s)", "grn")
              + f", logged {r['digest']['decision_count']} decision(s)")
        st = cg.stats(DB)
        print(f"   {c('WRITE-BACK','mag')}→ code graph now: "
              + c(f"{st['files']} files, {st['symbols']} symbols, {st['calls']} calls", "grn"))

    # full structural re-parse so cross-file call edges + topology are exact
    hr(); print(c("\n  INGEST: full structural parse of what we built\n", "bold"))
    s = cg.build_from_repo(DB, SITE, REPO)
    print(f"   code graph: {c(str(s['files']),'grn')} files · "
          f"{c(str(s['symbols']),'grn')} symbols · {c(str(s['calls']),'grn')} call edges")

    # ask the graph about the code IT JUST BUILT
    print(c("\n  QUERY: who calls add_task() in the app we just built?", "bold"))
    try:
        callers = cg.find_callers(DB, "add_task", repo=REPO)
        names = [x.get("caller") or x.get("name") or x for x in (callers or [])]
        print("   callers of add_task:", c(", ".join(map(str, names)) or "(none yet)", "grn"))
    except Exception as e:
        print("   (find_callers:", e, ")")
    try:
        topo = cg.analyze_topology(DB, repo=REPO, top_god=3)
        gods = [f"{g.get('leaf')} ({g.get('caller_count')} callers)"
                for g in (topo.get("god_nodes") or [])][:3]
        print("   busiest symbols (god-nodes):", c(", ".join(gods) or "(none)", "grn"))
    except Exception as e:
        print("   (topology:", e, ")")

    # PHASE 4 — sleep
    hr(); print(c("\n  CONSOLIDATE (sleep): fold every session into long-term memory\n", "bold"))
    rep = k.consolidate(run_dream=True)
    print(f"   sessions consolidated: {c(str(rep['sessions_consolidated']),'grn')}"
          f"  ·  edits: {rep['total_edits']}  ·  decisions: {rep['total_decisions']}")
    hot = ", ".join(f"{h['path']}(x{h['sessions']})" for h in rep["hot_files"][:4])
    print(f"   hot files: {c(hot or '(none)','grn')}")
    print(f"   scratchpads archived: {rep['archived']}  ·  summary decision: "
          + c(str(rep['summary_decision'])[:16] + '…', 'yel'))

    # open the real artifact
    index = os.path.join(SITE, "web", "index.html")
    hr(); print(c("\n  DONE — opening the live website in your browser:", "bold"))
    print("   " + c(index, "cyan"))
    print(c("   (it's a fully working todo app — add/complete/delete, saved in localStorage)\n", "dim"))
    try:
        webbrowser.open("file:///" + index.replace("\\", "/"))
    except Exception:
        pass


if __name__ == "__main__":
    main()
