"""Demo Phase-3 causal_radius — verifying the full pipeline."""
import asyncio, json, sqlite3
from decisiongraph.workspace import Workspace
from decisiongraph.mcp_server import _h_causal_radius

async def main():
    ws = Workspace("p4jIEgrAJd33s3qH-DZbhQ", "storage/workspaces")
    print("storage_dir:", ws._dg.storage_dir)
    out = await _h_causal_radius(
        ws._dg, None, None,
        path="src/flask/ctx.py",
        repo="pallets/flask")
    safe = {k: (v if isinstance(v, (int, str, float, bool, type(None)))
                else len(v) if isinstance(v, (list, dict))
                else str(type(v).__name__))
            for k, v in out.items()}
    print(json.dumps(safe, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
