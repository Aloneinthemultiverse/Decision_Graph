"""Helper the OS calls after a build: ingest the built site INTO the workspace
the DG app reads. Uses the local-folder variant of the GitHub ingest path.

We open the workspace's DG instance directly (same storage_dir the server uses
for that token). The server's in-memory cache won't see the changes until it
reloads that workspace — but the files on disk are the persistent source of
truth, so a server restart picks them up.
"""
from __future__ import annotations
import os


def ingest_built_site(workspace_token: str, local_path: str, repo_label: str,
                      storage_root: str = "storage") -> dict:
    """Ingest `local_path` into the workspace at storage/workspaces/<token>/personal/."""
    ws_root = os.path.join(storage_root, "workspaces", workspace_token, "personal")
    if not os.path.isdir(os.path.dirname(ws_root)):
        return {"ok": False, "error": f"workspace dir not found: {ws_root}"}
    os.makedirs(ws_root, exist_ok=True)

    from .core import DecisionGraph
    from .codebase_local import ingest_local_path_v2
    dg = DecisionGraph(storage_dir=ws_root)
    return ingest_local_path_v2(dg, local_path, repo_label)
