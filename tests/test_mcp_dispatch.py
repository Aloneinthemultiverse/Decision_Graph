"""MCP tool dispatch tests (in-process, no subprocess)."""
import pytest, asyncio
from decisiongraph.mcp_server import TOOL_HANDLERS, invoke_tool


def test_tool_registry_has_expected_count():
    """30 tools: 27 base + update_files + semantic_stale + get_context_pack."""
    assert len(TOOL_HANDLERS) == 30


def test_handler_names_unique():
    names = list(TOOL_HANDLERS.keys())
    assert len(names) == len(set(names))


@pytest.fixture(scope="module")
def dg_stack(tmp_path_factory):
    from decisiongraph.core import DecisionGraph
    from decisiongraph.company import EnterpriseHub
    from decisiongraph.discussion import DiscussionManager
    dg = DecisionGraph(storage_dir=str(tmp_path_factory.mktemp("dg")))
    return dg, EnterpriseHub(), DiscussionManager()


def test_code_graph_stats_dispatches(dg_stack):
    dg, hub, dm = dg_stack
    r = asyncio.run(invoke_tool("code_graph_stats", {}, dg, hub, dm))
    assert isinstance(r, dict)
    assert "files" in r and "symbols" in r and "calls" in r
    assert "edge_buckets" in r


def test_unknown_tool_returns_error(dg_stack):
    dg, hub, dm = dg_stack
    r = asyncio.run(invoke_tool("definitely_not_a_tool", {}, dg, hub, dm))
    assert "error" in r


def test_topology_dispatches(dg_stack):
    dg, hub, dm = dg_stack
    r = asyncio.run(invoke_tool("topology", {"top_god": 3}, dg, hub, dm))
    assert isinstance(r, dict)
    assert "god_nodes" in r
    assert "surprising_connections" in r


def test_suggested_questions_dispatches(dg_stack):
    dg, hub, dm = dg_stack
    r = asyncio.run(invoke_tool("suggested_questions", {}, dg, hub, dm))
    assert "questions" in r
    assert isinstance(r["questions"], list)
