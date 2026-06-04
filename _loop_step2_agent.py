"""Step 2: Agent reads graph via MCP tools, produces actual code."""
import json, re, sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
import anthropic
from decisiongraph import code_graph as cg

REPO_PATH = Path(r"C:\Users\SUJITN~1\AppData\Local\Temp\loop_flask")
DB        = str(REPO_PATH / ".dg_code_graph.db")
REPO      = "pallets/flask"
TARGET    = "src/flask/ctx.py"
TASK = ("Add a new method `mark_dirty(self) -> None` to the AppContext class "
        "in src/flask/ctx.py. The method should set `self._dirty = True` "
        "and have a brief docstring. Also initialize `self._dirty = False` "
        "in AppContext.__init__.")

# ── Gather context via our MCP tools ───────────────────────────────────
target_src = (REPO_PATH / TARGET).read_text(encoding="utf-8", errors="replace")
br      = cg.blast_radius(DB, TARGET, repo=REPO, max_depth=2, strict=True)
callers = cg.find_callers(DB, "AppContext", repo=REPO, limit=8)
triage  = cg.triage_pr(DB, [TARGET], repo=REPO, max_hops=2)
ctx = {
    "blast_radius": {"affected_files": br["affected_files"][:10],
                      "total": br["total_affected"]},
    "AppContext_callers": [{"path": c["path"], "caller": c["caller"],
                              "bucket": c["bucket"]} for c in callers],
    "triage": {"risk_score": triage["risk_score"],
                "risk_tier":  triage["risk_tier"],
                "merge_hint": triage["merge_order_hint"]},
}

# ── Ask LLM ────────────────────────────────────────────────────────────
client = anthropic.Anthropic(
    api_key="test", base_url="http://localhost:8080",
    default_headers={"ngrok-skip-browser-warning": "true"},
)

prompt = (
    f"You are editing a Python file. Codebase context from our graph "
    f"intelligence:\n\n```json\n{json.dumps(ctx, indent=2)}\n```\n\n"
    f"Current `src/flask/ctx.py` source:\n```python\n{target_src}\n```\n\n"
    f"TASK: {TASK}\n\n"
    f"Output ONLY a single ```python fenced block``` containing the COMPLETE "
    f"new AppContext class (everything from `class AppContext` to the last "
    f"line of that class, before the next top-level statement). Do NOT "
    f"include text outside the code fence. Do NOT include other classes.")

print("[agent] calling LLM...")
r = client.messages.create(model="gemini-3-flash", max_tokens=12000,
    messages=[{"role": "user", "content": prompt}])

# extract any text block
raw = ""
for b in r.content:
    if getattr(b, "type", "") == "text":
        raw += getattr(b, "text", "")
print(f"[agent] response: {len(raw)} chars, stop={r.stop_reason}")

# extract code fence
m = re.search(r"```(?:python)?\n(.*?)```", raw, re.DOTALL)
if not m:
    print("FAIL: no code block in response.")
    print("--- raw output (first 1500 chars) ---")
    print(raw[:1500])
    sys.exit(1)
new_class_code = m.group(1).rstrip()
print(f"[agent] extracted {len(new_class_code)} chars of code")
print("--- first 30 lines of generated AppContext ---")
for ln in new_class_code.splitlines()[:30]:
    print(f"  {ln}")

Path(".bench/loop_agent_class.py").write_text(new_class_code, encoding="utf-8")
print(f"\nsaved → .bench/loop_agent_class.py")
