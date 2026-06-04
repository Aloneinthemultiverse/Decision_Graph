"""Phase 3 proof: marketplace catalog + hire flow (no billing, no currency).

Run: python -m pytest test_agent_marketplace.py -q
"""
import os
import pytest

from decisiongraph.agent_catalog import load_catalog, get_agent, CatalogError


def test_catalog_loads_bundled_agents():
    cat = load_catalog()
    ids = {a["id"] for a in cat}
    assert "summarizer-v1" in ids
    assert "topic-counter-v1" in ids
    for a in cat:
        assert a["available"] is True
        assert a["script_path"].endswith(".py")
        # safety: scripts must resolve inside the repo
        assert os.path.isfile(a["script_path"])


def test_catalog_public_fields_present():
    a = get_agent("summarizer-v1")
    for k in ("id", "name", "description", "suggested_topics", "tags",
             "script_path", "available"):
        assert k in a


def test_unknown_agent_id_raises():
    with pytest.raises(CatalogError):
        get_agent("nope-v999")


def test_script_path_cannot_escape_repo(tmp_path):
    # craft a malicious catalog that tries ../ escape; load_catalog must drop it
    bad = tmp_path / "agent_catalog.json"
    bad.write_text('{"agents":[{"id":"bad","name":"x","description":"",'
                   '"script":"../../etc/passwd"}]}')
    out = load_catalog(str(bad))
    assert out == []   # filtered out, didn't escape
