"""In-process MCP test — bypasses stdio plumbing, exercises the actual tool
registration code path that handles MCP requests. This is what every
tool-call from Claude Code would ultimately invoke.
"""
import asyncio, json
from decisiongraph.mcp_server import TOOL_HANDLERS, invoke_tool
from decisiongraph.core import DecisionGraph
from decisiongraph.company import EnterpriseHub
from decisiongraph.discussion import DiscussionManager

# 1) Tool catalogue
print(f"=== 1) {len(TOOL_HANDLERS)} tools registered in TOOL_HANDLERS ===")
for name in sorted(TOOL_HANDLERS.keys()):
    print(f"     - {name}")

# Verify the tool definitions match (the types.Tool list)
from decisiongraph import mcp_server
import inspect
src = inspect.getsource(mcp_server)
import re
tool_defs = re.findall(r'types\.Tool\(\s*name="([^"]+)"', src)
print(f"\n=== 2) {len(tool_defs)} tool definitions in types.Tool registry ===")
diff = set(tool_defs) ^ set(TOOL_HANDLERS.keys())
if diff:
    print(f"   mismatch between handlers and tool defs: {diff}")
else:
    print(f"   handler list matches tool definitions exactly")

# 3) Initialize state + dispatch a real tool call
print(f"\n=== 3) Dispatch real tool calls ===")
# Use the loop_flask DB we've been working with
import os
os.environ.setdefault("DG_STORAGE_DIR",
    "storage/workspaces/p4jIEgrAJd33s3qH-DZbhQ/personal")
dg = DecisionGraph(storage_dir=os.environ["DG_STORAGE_DIR"])
hub = EnterpriseHub()
dm = DiscussionManager()

async def call(name, args):
    return await invoke_tool(name, args, dg, hub, dm)

# Walk through several tools
samples = [
    ("code_graph_stats",     {}),
    ("find_callers",         {"name": "render_template"}),
    ("blast_radius",         {"path": "src/flask/ctx.py", "max_depth": 1}),
    ("topology",             {"top_god": 3, "top_surprise": 0}),
    ("triage_pr",            {"changed_files": ["src/flask/ctx.py"]}),
    ("rationales",           {"path": "src/flask/ctx.py"}),
    ("suggested_questions",  {}),
]

for name, args in samples:
    r = asyncio.run(call(name, args))
    err = r.get("error") if isinstance(r, dict) else None
    if err:
        print(f"   [{name:<25}] ERROR: {err[:80]}")
    else:
        # show a short summary
        if isinstance(r, dict):
            keys = list(r.keys())[:5]
            print(f"   [{name:<25}] OK: keys={keys}")
        elif isinstance(r, list):
            print(f"   [{name:<25}] OK: list len={len(r)}")
        else:
            print(f"   [{name:<25}] OK: {type(r).__name__}")
