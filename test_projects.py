"""Projects registry tests — multi-workspace per device.

Run: python -m pytest test_projects.py -q
"""
import os
import json
import tempfile
import shutil
import importlib

import pytest


@pytest.fixture()
def fresh_storage(monkeypatch):
    """Each test gets its own storage dir so projects.json is isolated."""
    d = tempfile.mkdtemp(prefix="dg_proj_test_")
    # patch decisiongraph.config.STORAGE_DIR to point at temp dir
    from decisiongraph import config
    monkeypatch.setattr(config, "STORAGE_DIR", d)
    # reimport projects module under the patched config
    from decisiongraph import projects as projects_mod
    importlib.reload(projects_mod)
    yield d, projects_mod
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def wsm(fresh_storage):
    from decisiongraph.workspace import WorkspaceManager
    storage, _ = fresh_storage
    return WorkspaceManager(base_dir=os.path.join(storage, "workspaces"))


# ── basic CRUD ──────────────────────────────────────────────────────────
def test_create_and_list(fresh_storage, wsm):
    _, projects = fresh_storage
    dev = projects.new_device_id()
    a = projects.create_project(dev, "ACME Pricing", wsm)
    b = projects.create_project(dev, "Personal Research", wsm)
    assert a["id"] != b["id"]
    assert a["ws_token"] != b["ws_token"]
    items = projects.list_projects(dev)
    assert [p["name"] for p in items] == ["ACME Pricing", "Personal Research"]


def test_rename(fresh_storage, wsm):
    _, projects = fresh_storage
    dev = projects.new_device_id()
    p = projects.create_project(dev, "Old Name", wsm)
    p2 = projects.rename_project(dev, p["id"], "New Name")
    assert p2["name"] == "New Name"
    assert p2["slug"] == "new-name"
    # original record updated
    assert projects.get_project(dev, p["id"])["name"] == "New Name"


def test_delete_is_soft(fresh_storage, wsm):
    _, projects = fresh_storage
    dev = projects.new_device_id()
    p = projects.create_project(dev, "x", wsm)
    ws_token = p["ws_token"]
    assert projects.delete_project(dev, p["id"]) is True
    assert projects.list_projects(dev) == []
    # the underlying workspace storage is NOT wiped
    ws_dir = os.path.join(wsm.base, ws_token)
    assert os.path.isdir(ws_dir)


def test_persistence_across_loads(fresh_storage, wsm):
    storage, projects = fresh_storage
    dev = projects.new_device_id()
    projects.create_project(dev, "p1", wsm)
    projects.create_project(dev, "p2", wsm)
    # raw json file contains both
    with open(os.path.join(storage, "projects.json")) as f:
        data = json.load(f)
    assert len(data[dev]) == 2


# ── isolation ───────────────────────────────────────────────────────────
def test_projects_isolated_across_devices(fresh_storage, wsm):
    _, projects = fresh_storage
    dev_a, dev_b = projects.new_device_id(), projects.new_device_id()
    pa = projects.create_project(dev_a, "A's project", wsm)
    pb = projects.create_project(dev_b, "B's project", wsm)
    # device A should not see device B's projects (or vice versa)
    a_list = [p["name"] for p in projects.list_projects(dev_a)]
    b_list = [p["name"] for p in projects.list_projects(dev_b)]
    assert a_list == ["A's project"]
    assert b_list == ["B's project"]


def test_workspace_tokens_unique(fresh_storage, wsm):
    _, projects = fresh_storage
    dev = projects.new_device_id()
    tokens = {projects.create_project(dev, f"p{i}", wsm)["ws_token"]
              for i in range(5)}
    assert len(tokens) == 5


# ── adopt + migration ───────────────────────────────────────────────────
def test_adopt_existing_workspace_idempotent(fresh_storage, wsm):
    _, projects = fresh_storage
    dev = projects.new_device_id()
    # simulate: user already has a ws_token from old cookie
    legacy_token = wsm.new_token()
    p1 = projects.adopt_existing_workspace(dev, legacy_token,
                                            name="Default Project")
    p2 = projects.adopt_existing_workspace(dev, legacy_token,
                                            name="Different Name")
    # second call should not create a duplicate
    assert p1["id"] == p2["id"]
    assert p1["adopted"] is True
    assert len(projects.list_projects(dev)) == 1


# ── resolve_active_project (the middleware lookup) ──────────────────────
def test_resolve_no_cookies_creates_default(fresh_storage, wsm):
    _, projects = fresh_storage
    r = projects.resolve_active_project(None, None, None, wsm)
    assert r["created"] is True
    assert r["project"]["name"] == "Default Project"
    assert r["device_id"].startswith("dev_")


def test_resolve_with_legacy_ws_cookie_adopts(fresh_storage, wsm):
    _, projects = fresh_storage
    legacy_token = wsm.new_token()
    r = projects.resolve_active_project(None, None, legacy_token, wsm)
    assert r["adopted"] is True
    assert r["project"]["ws_token"] == legacy_token
    assert r["project"]["name"] == "Default Project"


def test_resolve_picks_requested_project(fresh_storage, wsm):
    _, projects = fresh_storage
    dev = projects.new_device_id()
    p1 = projects.create_project(dev, "p1", wsm)
    p2 = projects.create_project(dev, "p2", wsm)
    r = projects.resolve_active_project(dev, p2["id"], None, wsm)
    assert r["project"]["id"] == p2["id"]


def test_resolve_falls_back_to_most_recent(fresh_storage, wsm):
    _, projects = fresh_storage
    dev = projects.new_device_id()
    projects.create_project(dev, "older", wsm)
    p2 = projects.create_project(dev, "newer", wsm)
    # no requested project — should pick the most recent
    r = projects.resolve_active_project(dev, None, None, wsm)
    assert r["project"]["id"] == p2["id"]


def test_resolve_invalid_requested_id_falls_back(fresh_storage, wsm):
    _, projects = fresh_storage
    dev = projects.new_device_id()
    p = projects.create_project(dev, "real", wsm)
    # ask for a project_id that doesn't exist for this device
    r = projects.resolve_active_project(dev, "p_nonexistent", None, wsm)
    # should fall through to "use most recent" — p
    assert r["project"]["id"] == p["id"]
