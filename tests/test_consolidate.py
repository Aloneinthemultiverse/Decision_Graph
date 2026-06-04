"""Phase 4 — Consolidation ("sleep"): drain KERNEL-CAG → DG long-term memory."""
import os
import json
import pytest

from decisiongraph.core import DecisionGraph
from decisiongraph.kernel import Kernel
from decisiongraph.consolidate import consolidate, _scan_ended


_BLUEPRINT = """# Project: acme/widget

## Mission

Widget handles authentication and user records.

# Files in acme/widget

## File: src/api/auth.py

The file `src/api/auth.py` lives in folder `src/api/`. Handles auth and login.
"""


@pytest.fixture
def kit(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    bp = storage / "blueprints"
    bp.mkdir()
    (bp / "acme_widget.md").write_text(_BLUEPRINT, encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "auth-builder.md").write_text(
        "---\nname: auth-builder\n"
        "description: Builds authentication and login features.\n"
        'tools: ["Read", "Write", "Edit"]\n---\nbody', encoding="utf-8")
    dg = DecisionGraph(storage_dir=str(storage))
    k = Kernel(dg, str(ws), str(agents), storage_dir=str(storage))
    return k, str(storage), dg


def _ex(contract):
    return {
        "edits": [{"path": "src/api/auth.py", "summary": "x", "text": "def f(): pass"}],
        "decisions": [{"question": "rotate tokens?", "answer": "yes",
                       "reasoning": "security"}],
        "notes": ["note"],
    }


def test_no_sessions_is_noop(kit):
    k, storage, _ = kit
    r = k.consolidate(run_dream=False)
    assert r["sessions_consolidated"] == 0
    assert "no ended" in r.get("note", "")


def test_consolidate_aggregates_and_archives(kit):
    k, storage, dg = kit
    # produce two ended sessions via run_task (each closes its scratchpad)
    k.run_task("build auth login", task_files=["src/api/auth.py"],
               repo="acme/widget", executor=_ex)
    k.run_task("fix auth login", task_files=["src/api/auth.py"],
               repo="acme/widget", executor=_ex)

    hot = os.path.join(storage, "kernel_cag")
    ended_before = _scan_ended(storage)
    assert len(ended_before) == 2

    r = k.consolidate(run_dream=False)
    assert r["sessions_consolidated"] == 2
    assert r["total_edits"] == 2
    assert r["total_decisions"] == 2
    # auth.py churned in both sessions → top hot file
    assert r["hot_files"][0]["path"] == "src/api/auth.py"
    assert r["hot_files"][0]["sessions"] == 2
    # a consolidation summary decision was written
    assert r["summary_decision"]
    # scratchpads moved out of the hot dir into _archive
    assert r["archived"] == 2
    assert _scan_ended(storage) == []
    assert os.path.isdir(os.path.join(hot, "_archive"))


def test_consolidate_is_idempotent(kit):
    k, storage, _ = kit
    k.run_task("build auth", task_files=["src/api/auth.py"],
               repo="acme/widget", executor=_ex)
    r1 = k.consolidate(run_dream=False)
    assert r1["sessions_consolidated"] == 1
    # second pass: nothing left in the hot dir
    r2 = k.consolidate(run_dream=False)
    assert r2["sessions_consolidated"] == 0


def test_running_session_not_consolidated(kit):
    k, storage, _ = kit
    from decisiongraph.kernel_cag import KernelCAG
    live = KernelCAG(storage)              # created but NOT closed (ended is None)
    live.append("edit", {"path": "a.py"})
    r = k.consolidate(run_dream=False)
    assert r["sessions_consolidated"] == 0  # live session left untouched
    assert os.path.exists(live.path)


def test_dream_runs_without_workspace(kit):
    k, storage, _ = kit
    k.run_task("build auth", task_files=["src/api/auth.py"],
               repo="acme/widget", executor=_ex)
    r = k.consolidate(run_dream=True)       # no workspace → mem-level fallback
    assert r["dream"] is not None
    assert "error" not in (r["dream"] or {})
