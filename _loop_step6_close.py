"""Step 6: ask the agent about the just-added mark_dirty().
Goal: confirm the agent can RETRIEVE its own change from the graph in a
NEW session that has zero memory of the prior conversation.
"""
import json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
import anthropic
from decisiongraph import code_graph as cg

REPO_PATH = Path(r"C:\Users\SUJITN~1\AppData\Local\Temp\loop_flask")
DB        = str(REPO_PATH / ".dg_code_graph.db")
REPO      = "pallets/flask"

# Pull whatever the graph knows about mark_dirty
sym_info = cg.find_callers(DB, "mark_dirty", repo=REPO, limit=10)
import sqlite3
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
sym_row = conn.execute(
    "SELECT path, qualified_name, start_line, end_line, kind "
    "FROM symbols WHERE name='mark_dirty'").fetchone()
conn.close()

graph_signals = {
    "found_in_graph":  bool(sym_row),
    "path":            sym_row["path"]            if sym_row else None,
    "qualified_name":  sym_row["qualified_name"]  if sym_row else None,
    "lines":           [sym_row["start_line"], sym_row["end_line"]] if sym_row else None,
    "current_callers": len(sym_info),
}
print(f"\n[graph signals fed to fresh agent]")
print(json.dumps(graph_signals, indent=2))

# Now ask the agent — pretend this is a brand-new developer session
client = anthropic.Anthropic(
    api_key="test", base_url="http://localhost:8080",
    default_headers={"ngrok-skip-browser-warning": "true"},
)
prompt = (
    f"You are answering a developer who just joined the project. They ask: "
    f"\"Is there a method called `mark_dirty` in this codebase? If yes, where "
    f"is it defined, what does it do, and is anyone using it?\"\n\n"
    f"You have these graph signals from the DecisionGraph code intelligence "
    f"layer:\n\n```json\n{json.dumps(graph_signals, indent=2)}\n```\n\n"
    f"Answer in 3-4 sentences max. Cite the graph fields you used.")

print("\n[agent] calling LLM...")
r = client.messages.create(model="gemini-3-flash", max_tokens=4000,
    messages=[{"role": "user", "content": prompt}])
text_blocks = [getattr(b, "text", "") for b in r.content
                if getattr(b, "type", "") == "text"]
answer = "\n".join(t for t in text_blocks if t).strip()
print(f"[agent] {r.usage.output_tokens} output tokens")
print("\n" + "=" * 70)
print("AGENT ANSWER:")
print("=" * 70)
print(answer)
print("=" * 70)

# Spot-check: does the answer cite the file path the graph reported?
expected_path = sym_row["path"] if sym_row else ""
expected_class = "AppContext"
cites_path  = expected_path.lower() in answer.lower()
cites_class = expected_class.lower() in answer.lower()
print(f"\nVALIDATION:")
print(f"  agent cites path '{expected_path}': {cites_path}")
print(f"  agent cites class '{expected_class}': {cites_class}")
print(f"  agent acknowledges 0 callers: "
      f"{('0' in answer or 'no callers' in answer.lower() or 'not' in answer.lower())}")
