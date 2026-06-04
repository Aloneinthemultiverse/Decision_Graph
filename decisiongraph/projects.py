"""Projects = named workspaces for one device.

Today the system has one workspace per browser cookie (`dg_ws`). Projects
sit one layer above: ONE device (identified by `dg_device` cookie) can own
MANY projects, each backed by its own workspace token. Each project gets
fully-isolated knowledge graph, decision memory, sessions, agent grants,
audit — because each is just a normal workspace under the hood.

Backwards-compatible: an existing `dg_ws` cookie auto-promotes the user's
existing workspace into a project named "Default Project". Nothing is lost.

Storage: a single JSONL-ish registry at storage/projects.json that maps
  { device_id -> [ { id, name, ws_token, created_at, deleted, ... } ] }

stdlib only, thread-safe.
"""
from __future__ import annotations

import os
import json
import time
import secrets
import threading
from pathlib import Path
from typing import Optional

from . import config
from .workspace import WorkspaceManager


_LOCK = threading.RLock()
_REGISTRY_FILE = "projects.json"


def _registry_path() -> str:
    return os.path.join(config.STORAGE_DIR, _REGISTRY_FILE)


def _load_all() -> dict:
    p = _registry_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_all(data: dict) -> None:
    p = _registry_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def _slugify(name: str) -> str:
    """A URL-friendly project id derived from a name."""
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s or "project")[:40]


def new_device_id() -> str:
    return "dev_" + secrets.token_urlsafe(12)


def new_project_id(existing: set[str]) -> str:
    """A short, unique-within-device project id."""
    base = "p_" + secrets.token_urlsafe(8)
    while base in existing:
        base = "p_" + secrets.token_urlsafe(8)
    return base


def list_projects(device_id: str) -> list[dict]:
    """All non-deleted projects for this device, in creation order."""
    with _LOCK:
        all_data = _load_all()
        projects = all_data.get(device_id) or []
        return [p for p in projects if not p.get("deleted")]


def get_project(device_id: str, project_id: str) -> Optional[dict]:
    with _LOCK:
        for p in (_load_all().get(device_id) or []):
            if p.get("id") == project_id and not p.get("deleted"):
                return p
    return None


def create_project(device_id: str, name: str,
                   wsm: WorkspaceManager, seed: bool = False) -> dict:
    """Create a fresh project = fresh workspace. Mints a new ws_token.

    `seed` only True for the user's FIRST/default project on a fresh device —
    so they land on a pre-populated demo graph. Every subsequent project the
    user creates explicitly starts empty (their own real work)."""
    name = (name or "").strip() or "Untitled Project"
    with _LOCK:
        all_data = _load_all()
        existing = {p["id"] for p in (all_data.get(device_id) or [])}
        pid = new_project_id(existing)
        ws_token = wsm.new_token()
        # touch the workspace so its dir is created — seed only if explicitly asked
        wsm.get(ws_token, seed=seed)
        rec = {
            "id": pid,
            "slug": _slugify(name),
            "name": name,
            "ws_token": ws_token,
            "created_at": time.time(),
            "deleted": False,
        }
        all_data.setdefault(device_id, []).append(rec)
        _save_all(all_data)
        return rec


def adopt_existing_workspace(device_id: str, ws_token: str,
                              name: str = "Default Project") -> dict:
    """Promote an already-existing workspace token into a project record.
    Used for migration: if a user already has a workspace via the old
    `dg_ws` cookie, we wrap it as their first project rather than wipe."""
    with _LOCK:
        all_data = _load_all()
        projects = all_data.setdefault(device_id, [])
        # idempotent: if this ws_token is already a project, return it
        for p in projects:
            if p.get("ws_token") == ws_token and not p.get("deleted"):
                return p
        existing = {p["id"] for p in projects}
        pid = new_project_id(existing)
        rec = {
            "id": pid,
            "slug": _slugify(name),
            "name": name,
            "ws_token": ws_token,
            "created_at": time.time(),
            "deleted": False,
            "adopted": True,
        }
        projects.append(rec)
        _save_all(all_data)
        return rec


def rename_project(device_id: str, project_id: str, new_name: str) -> Optional[dict]:
    new_name = (new_name or "").strip()
    if not new_name:
        return None
    with _LOCK:
        all_data = _load_all()
        for p in (all_data.get(device_id) or []):
            if p.get("id") == project_id and not p.get("deleted"):
                p["name"] = new_name
                p["slug"] = _slugify(new_name)
                _save_all(all_data)
                return p
    return None


def delete_project(device_id: str, project_id: str) -> bool:
    """Soft-delete. The workspace storage is preserved on disk — only the
    project record is marked deleted. Can be undeleted manually by editing
    projects.json if needed."""
    with _LOCK:
        all_data = _load_all()
        for p in (all_data.get(device_id) or []):
            if p.get("id") == project_id and not p.get("deleted"):
                p["deleted"] = True
                p["deleted_at"] = time.time()
                _save_all(all_data)
                return True
    return False


def resolve_active_project(device_id: Optional[str],
                            requested_project_id: Optional[str],
                            ws_cookie: Optional[str],
                            wsm: WorkspaceManager) -> dict:
    """The middleware's lookup. Returns the resolved project dict (with
    ws_token), creating/adopting as needed:

      1. If we have a device_id and a requested project_id that exists -> use it.
      2. Else if we have device + existing projects -> use the first one.
      3. Else if there's an old-style ws_cookie -> adopt it as Default Project.
      4. Else -> create a fresh Default Project from scratch.

    The caller is responsible for setting/refreshing the device_id cookie if
    we minted one (we return the device id we used).
    """
    if not device_id:
        device_id = new_device_id()

    # 1. explicit request
    if requested_project_id:
        proj = get_project(device_id, requested_project_id)
        if proj:
            return {"device_id": device_id, "project": proj, "created": False}

    # 2. existing projects for this device — pick the most recent
    projects = list_projects(device_id)
    if projects:
        proj = projects[-1]
        return {"device_id": device_id, "project": proj, "created": False}

    # 3. adopt an old-style workspace if present
    if ws_cookie and WorkspaceManager._safe(ws_cookie):
        proj = adopt_existing_workspace(device_id, ws_cookie)
        return {"device_id": device_id, "project": proj, "created": True,
                "adopted": True}

    # 4. fresh new device + fresh new default project (only THIS one gets seeded)
    proj = create_project(device_id, "Default Project", wsm, seed=True)
    return {"device_id": device_id, "project": proj, "created": True}
