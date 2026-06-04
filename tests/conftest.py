"""Shared pytest fixtures."""
import os, tempfile, shutil
import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory):
    """Shared fixture dir for synthetic source files."""
    d = tmp_path_factory.mktemp("fixtures")
    return d


@pytest.fixture
def empty_db_path(tmp_path):
    """Path to a fresh, unused code_graph.db."""
    return str(tmp_path / "code_graph.db")


@pytest.fixture
def tiny_repo(tmp_path):
    """Minimal python repo: 2 files, 1 class with 1 method."""
    (tmp_path / "a.py").write_text(
        "def helper(x):\n    return x + 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "from a import helper\n\nclass Calculator:\n"
        "    # NOTE: simple wrapper\n"
        "    def compute(self, n):\n"
        "        return helper(n) * 2\n", encoding="utf-8")
    return tmp_path
