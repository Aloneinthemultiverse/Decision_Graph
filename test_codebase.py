"""Shape B (codebase ingestion + context retrieval) tests.

Run: python -m pytest test_codebase.py -q
"""
import os
import shutil
import subprocess
import tempfile
import pytest

from decisiongraph.codebase import (
    ingest_repo, get_codebase_context, _python_module_docstring,
    _safe_read, _repo_name)


@pytest.fixture()
def fake_repo():
    """Create a fake git repo with README, an ADR, a Python file with a
    docstring, a manifest, and 2 commits."""
    d = tempfile.mkdtemp(prefix="dg_repo_")
    subprocess.run(["git", "init"], cwd=d, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@x.com"], cwd=d,
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=d,
                   capture_output=True, check=True)
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write("# Sample Repo\n\nThis is a test repo for DG ingestion.\n")
    os.makedirs(os.path.join(d, "docs", "adr"), exist_ok=True)
    with open(os.path.join(d, "docs", "adr", "0001-use-jwt.md"), "w") as f:
        f.write("# ADR-1: Use JWT for auth\n\nDecision: use JWT. Reason: stateless.")
    with open(os.path.join(d, "pyproject.toml"), "w") as f:
        f.write('[project]\nname = "sample"\nversion = "0.1.0"\n')
    with open(os.path.join(d, "main.py"), "w") as f:
        f.write('"""Main module. Handles authentication and routing."""\n\ndef foo():\n    pass\n')
    subprocess.run(["git", "add", "-A"], cwd=d, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit", "--no-verify"],
                   cwd=d, capture_output=True)
    with open(os.path.join(d, "main.py"), "a") as f:
        f.write("\ndef bar():\n    pass\n")
    subprocess.run(["git", "add", "-A"], cwd=d, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add bar function", "--no-verify"],
                   cwd=d, capture_output=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


class _FakeMem:
    def __init__(self): self.stored = []
    def store(self, **kw):
        did = f"D{len(self.stored)}"
        self.stored.append({"id": did, **kw}); return did
    def save(self): pass
    def get_active_decisions(self):
        return [{"id": d["id"], "question": d["question"],
                 "answer": d["answer"], "confidence": 0.9,
                 "timestamp": "2026-01-01"} for d in self.stored]


class _FakeDG:
    def __init__(self): self.memory = _FakeMem()


# ── unit helpers ────────────────────────────────────────────────────────
def test_python_module_docstring_extraction():
    src = '"""This is the docstring."""\n\ndef foo(): pass\n'
    assert _python_module_docstring(src) == "This is the docstring."
    assert _python_module_docstring("def foo(): pass") is None
    assert _python_module_docstring("# just a comment") is None


def test_safe_read_handles_big_files(tmp_path):
    p = tmp_path / "big.txt"
    p.write_bytes(b"x" * 1_000_000)
    assert _safe_read(p, max_bytes=10_000) is None
    p.write_text("small")
    assert _safe_read(p) == "small"


# ── ingest_repo ─────────────────────────────────────────────────────────
def test_ingest_repo_collects_artifacts(fake_repo):
    dg = _FakeDG()
    summary = ingest_repo(dg, fake_repo)
    assert "error" not in summary
    assert summary["stored"] > 0
    # we expect: README (doc), ADR, manifest, module_docstring, 2 commits
    assert summary["doc"] >= 1
    assert summary["adr"] >= 1
    assert summary["manifest"] >= 1
    assert summary["module_docstring"] >= 1
    assert summary["commit"] >= 2
    # all stored entries are tagged with [repo:...]
    for entry in dg.memory.stored:
        assert entry["question"].startswith("[repo:")


def test_ingest_repo_rejects_non_repo(tmp_path):
    out = ingest_repo(_FakeDG(), str(tmp_path))
    assert "error" in out


def test_ingest_repo_can_skip_commits(fake_repo):
    dg = _FakeDG()
    summary = ingest_repo(dg, fake_repo, include_commits=False)
    assert summary["commit"] == 0
    assert summary["doc"] >= 1


def test_ingest_repo_can_skip_files(fake_repo):
    dg = _FakeDG()
    summary = ingest_repo(dg, fake_repo, include_files=False)
    assert summary["doc"] == 0
    assert summary["commit"] >= 2


# ── get_codebase_context ────────────────────────────────────────────────
def test_get_codebase_context_finds_relevant_decisions(fake_repo):
    dg = _FakeDG()
    ingest_repo(dg, fake_repo)
    repo = _repo_name(__import__("pathlib").Path(fake_repo))

    # When editing main.py with auth intent, the ADR + the module docstring
    # should both surface
    out = get_codebase_context(dg, file_path="main.py",
                                intent="working on authentication",
                                repo_name=repo, limit=10)
    assert "hits" in out
    assert len(out["hits"]) > 0
    # Top hit should reference either the ADR or the docstring (both auth)
    blob = " ".join(h["question"] + " " + h["answer"] for h in out["hits"]).lower()
    assert "jwt" in blob or "authentication" in blob


def test_get_codebase_context_empty_when_no_match():
    dg = _FakeDG()
    out = get_codebase_context(dg, file_path="anything.py",
                                intent="totally unrelated",
                                limit=5)
    assert out["hits"] == []
    assert "No institutional context" in out["context"]


def test_context_block_is_paste_ready():
    dg = _FakeDG()
    dg.memory.stored = [
        {"id": "D0", "question": "[repo:foo] adr · use JWT",
         "answer": "Decision: use JWT for auth"},
    ]
    out = get_codebase_context(dg, file_path="auth.py", intent="login",
                                limit=5)
    ctx = out["context"]
    assert "INSTITUTIONAL CONTEXT" in ctx
    assert "[1]" in ctx
    assert "JWT" in ctx
