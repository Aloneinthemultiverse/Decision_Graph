"""Phase 3 — the Kernel: dispatch → load → grant → run+log (full chain)."""
import os
import json
import pytest

from decisiongraph.core import DecisionGraph
from decisiongraph.kernel import Kernel, load_agents, _parse_frontmatter, _significant_words


_ECC_AGENTS = os.path.join(os.path.dirname(__file__), "..", "ecc", "agents")
_HAVE_ECC = os.path.isdir(_ECC_AGENTS)

_BLUEPRINT = """# Project: acme/widget

## Mission

Widget does things. Handles authentication and user records.

## Key Concepts

- The project implements **frobnication** and login security.


# Files in acme/widget

## File: src/api/auth.py

The file `src/api/auth.py` lives in folder `src/api/`. Handles auth and login security.
It defines: `login`, `logout`.

## File: src/api/users.py

The file `src/api/users.py` lives in folder `src/api/`. User CRUD.

## File: src/db/models.py

The file `src/db/models.py` lives in folder `src/db/`. ORM models.

# Recent activity in acme/widget

- alice committed: fix auth bug
"""


@pytest.fixture
def stack(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    bp_dir = storage / "blueprints"
    bp_dir.mkdir()
    (bp_dir / "acme_widget.md").write_text(_BLUEPRINT, encoding="utf-8")

    ws_root = tmp_path / "ws"
    ws_root.mkdir()

    dg = DecisionGraph(storage_dir=str(storage))
    agents_dir = _ECC_AGENTS if _HAVE_ECC else str(_make_fake_agents(tmp_path))
    k = Kernel(dg, str(ws_root), agents_dir, storage_dir=str(storage))
    return k, str(storage)


def _make_fake_agents(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "code-reviewer.md").write_text(
        "---\nname: code-reviewer\n"
        "description: Reviews code for security and quality. Use PROACTIVELY for careful audit.\n"
        'tools: ["Read", "Grep"]\nmodel: sonnet\n---\nbody', encoding="utf-8")
    (d / "auth-builder.md").write_text(
        "---\nname: auth-builder\n"
        "description: Builds authentication and login user records features.\n"
        'tools: ["Read", "Write", "Edit"]\nmodel: sonnet\n---\nbody', encoding="utf-8")
    return d


# ── unit: frontmatter / agent loading ────────────────────────────────────────

def test_parse_frontmatter_handles_lists():
    fm = _parse_frontmatter('---\nname: x\ntools: ["A", "B"]\n---\nbody')
    assert fm["name"] == "x"
    assert fm["tools"] == ["A", "B"]


def test_significant_words_drops_stopwords():
    w = _significant_words("Use PROACTIVELY to review the authentication code")
    assert "authentication" in w and "review" in w
    assert "the" not in w and "use" not in w


def test_load_agents_nonempty(stack):
    k, _ = stack
    assert len(k.agents) >= 2
    assert all("name" in a and "_words" in a for a in k.agents)


# ── JOB 1: DISPATCH ───────────────────────────────────────────────────────────

def test_dispatch_picks_keyword_relevant_agent(stack):
    k, _ = stack
    d = k.dispatch("build authentication login feature for user records",
                   task_files=["src/api/auth.py"])
    assert d["agent"] is not None
    assert d["score"] > 0
    assert len(d["candidates"]) >= 1
    # the winning agent's description should share keywords with the task
    task_words = _significant_words("build authentication login feature for user records")
    top = next(a for a in k.agents if a["name"] == d["agent"])
    assert top["_words"] & task_words


def test_dispatch_high_risk_boosts_careful_agent(stack):
    k, _ = stack
    # force high_risk; a careful/review/audit agent should be favored
    d = k.dispatch("change core module", high_risk=True)
    assert d["high_risk"] is True
    top = next(a for a in k.agents if a["name"] == d["agent"])
    assert any(h in top["name"].lower() or h in top["description"].lower()
               for h in ("review", "architect", "security", "audit", "careful", "refactor"))


# ── JOB 2: LOAD ───────────────────────────────────────────────────────────────

def test_load_preloads_context_slice(stack):
    k, _ = stack
    cag, pack = k.load(["src/api/auth.py"], repo="acme/widget")
    assert "src/api/auth.py" in pack["included_files"]
    assert cag.get_context_slice()["included_files"] == pack["included_files"]
    assert "Mission" in pack["markdown"]


# ── JOB 3: GRANT ────────────────────────────────────────────────────────────

def test_grant_scopes_topics_and_write(stack):
    k, _ = stack
    agent = {"name": "auth-builder", "tools": ["Read", "Write", "Edit"]}
    g = k.grant(agent, repo="acme/widget", task_files=["src/api/auth.py"])
    assert g["can_write"] is True
    assert "acme/widget" in g["allowed_topics"]
    assert "src/api" in g["allowed_topics"]
    assert g["token"].startswith("ag_")

    ro = {"name": "code-reviewer", "tools": ["Read", "Grep"]}
    g2 = k.grant(ro, repo="acme/widget")
    assert g2["can_write"] is False


# ── JOB 4: RUN + LOG (full chain) ──────────────────────────────────────────

def test_run_task_full_chain_with_fake_executor(stack):
    k, storage = stack
    calls = {}

    def executor(contract):
        calls["contract"] = contract
        return {
            "tool_calls": [{"tool": "Read", "path": "src/api/auth.py"}],
            "edits": [{"path": "src/api/auth.py", "summary": "add refresh token",
                       "text": "def refresh(): pass"}],
            "decisions": [{"question": "use rotating refresh tokens?",
                           "answer": "yes", "reasoning": "security best practice"}],
            "notes": ["watch token expiry"],
        }

    r = k.run_task("build authentication login refresh for user records",
                   task_files=["src/api/auth.py"], repo="acme/widget",
                   executor=executor)

    assert r["status"] == "job_completed"
    # executor received a real contract with the loaded slice + grant
    assert "context_markdown" in calls["contract"]
    assert calls["contract"]["grant_token"].startswith("ag_")
    # digest reflects the logged work
    assert r["digest"]["edit_count"] == 1
    assert r["digest"]["decision_count"] == 1
    assert "src/api/auth.py" in r["digest"]["edited_files"]
    # write-back stored the decision into DG memory
    assert r["writeback"]["decisions_stored"] == 1

    # audit log captured the lifecycle
    audit = k.access.read_audit()
    events = [a["event"] for a in audit]
    assert "grant_minted" in events
    assert "job_started" in events
    assert "job_completed" in events


def test_run_task_executor_failure_marks_failed(stack):
    k, _ = stack

    def boom(contract):
        raise RuntimeError("agent crashed")

    r = k.run_task("do something", task_files=["src/api/auth.py"],
                   repo="acme/widget", executor=boom)
    assert r["status"] == "job_failed"
    assert "agent crashed" in r["result"]["error"]
    events = [a["event"] for a in k.access.read_audit()]
    assert "job_failed" in events


def test_run_task_no_executor_is_dry_run(stack):
    k, _ = stack
    r = k.run_task("inspect auth", task_files=["src/api/auth.py"], repo="acme/widget")
    assert r["status"] == "job_completed"
    assert r["result"]["edits"] == []
    assert r["digest"]["edit_count"] == 0
