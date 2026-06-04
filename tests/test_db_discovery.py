"""DB auto-discovery for the MCP server."""
import os
import pytest
from pathlib import Path
from decisiongraph.mcp_server import _cg_db_path


class FakeDG:
    def __init__(self, storage_dir):
        self.storage_dir = storage_dir


def test_env_override_wins_even_if_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("DG_CODE_GRAPH_DB", "Z:/explicit/path.db")
    assert _cg_db_path(FakeDG(str(tmp_path))) == "Z:/explicit/path.db"


def test_cwd_db_picked_when_present(monkeypatch, tmp_path):
    monkeypatch.delenv("DG_CODE_GRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".dg_code_graph.db").write_bytes(b"")
    expected = str(tmp_path / ".dg_code_graph.db")
    assert _cg_db_path(FakeDG("storage")) == expected


def test_workspace_storage_dir_used(monkeypatch, tmp_path):
    monkeypatch.delenv("DG_CODE_GRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)    # no cwd db
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "code_graph.db").write_bytes(b"")
    assert _cg_db_path(FakeDG(str(ws))) == str(ws / "code_graph.db")


def test_fallback_returns_storage_path(monkeypatch, tmp_path):
    monkeypatch.delenv("DG_CODE_GRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / "nonexistent_ws"
    expected = str(ws / "code_graph.db")
    assert _cg_db_path(FakeDG(str(ws))) == expected
