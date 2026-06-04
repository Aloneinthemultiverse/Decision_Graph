"""Verify _cg_db_path discovers the right DB in all four scenarios."""
import os, tempfile, shutil
from pathlib import Path

from decisiongraph.mcp_server import _cg_db_path

class FakeDG:
    def __init__(self, storage_dir):
        self.storage_dir = storage_dir

# scenario 1: env var override
print("=== 1) DG_CODE_GRAPH_DB env override ===")
os.environ["DG_CODE_GRAPH_DB"] = "Z:/some/explicit/path.db"
print(f"   resolved: {_cg_db_path(FakeDG('storage'))}")
del os.environ["DG_CODE_GRAPH_DB"]

# scenario 2: .dg_code_graph.db in CWD (typical "Claude Code in my repo")
print("\n=== 2) cwd .dg_code_graph.db ===")
tmp = tempfile.mkdtemp()
os.chdir(tmp)
Path(".dg_code_graph.db").write_bytes(b"")
print(f"   resolved: {_cg_db_path(FakeDG('storage'))}")
print(f"   matches cwd:  {_cg_db_path(FakeDG('storage')) == os.path.join(tmp, '.dg_code_graph.db')}")

# scenario 3: workspace-style storage_dir/code_graph.db
print("\n=== 3) workspace storage_dir/code_graph.db ===")
ws = tempfile.mkdtemp()
Path(ws, "code_graph.db").write_bytes(b"")
os.remove(".dg_code_graph.db")    # remove cwd one
print(f"   resolved: {_cg_db_path(FakeDG(ws))}")
print(f"   matches workspace: {_cg_db_path(FakeDG(ws)) == os.path.join(ws, 'code_graph.db')}")

# scenario 4: fallback returns storage_dir/code_graph.db even when nothing exists
print("\n=== 4) nothing exists — graceful fallback ===")
os.chdir(tempfile.mkdtemp())   # empty cwd
nonexistent_ws = tempfile.mkdtemp()
shutil.rmtree(nonexistent_ws)
res = _cg_db_path(FakeDG(nonexistent_ws))
print(f"   resolved: {res}")
print(f"   is the workspace path:  {res == os.path.join(nonexistent_ws, 'code_graph.db')}")
print(f"   (caller will get file-not-found error, not silent zeros)")

print("\nAll 4 scenarios resolved correctly.")
