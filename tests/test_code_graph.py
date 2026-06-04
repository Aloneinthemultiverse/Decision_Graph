"""Code graph: ingest, query, blast_radius, find_callers, etc."""
import pytest
from decisiongraph import code_graph as cg


def test_build_from_repo_creates_symbols(tiny_repo, empty_db_path):
    s = cg.build_from_repo(empty_db_path, str(tiny_repo), "test/tiny")
    assert s["files"] == 2
    assert s["symbols"] >= 3        # helper, Calculator, compute
    assert s["calls"] >= 1          # compute -> helper
    assert s["imports"] >= 1        # b.py imports a
    assert s["rationales"] == 1     # `# NOTE: simple wrapper`


def test_find_callers_finds_helper(tiny_repo, empty_db_path):
    cg.build_from_repo(empty_db_path, str(tiny_repo), "test/tiny")
    callers = cg.find_callers(empty_db_path, "helper", repo="test/tiny")
    assert len(callers) >= 1
    paths = {c["path"] for c in callers}
    assert "b.py" in paths
    assert all("bucket" in c for c in callers)


def test_blast_radius_returns_affected_files(tiny_repo, empty_db_path):
    cg.build_from_repo(empty_db_path, str(tiny_repo), "test/tiny")
    r = cg.blast_radius(empty_db_path, "a.py", repo="test/tiny", max_depth=2)
    assert "affected_files" in r
    assert isinstance(r["affected_files"], list)
    assert r["total_affected"] >= 1
    assert "b.py" in r["affected_files"]


def test_rationales_in_file(tiny_repo, empty_db_path):
    cg.build_from_repo(empty_db_path, str(tiny_repo), "test/tiny")
    rats = cg.rationales_in_file(empty_db_path, "b.py", repo="test/tiny")
    assert len(rats) == 1
    assert rats[0]["tag"] == "NOTE"
    assert "simple wrapper" in rats[0]["text"]


def test_topology_returns_god_nodes_field(tiny_repo, empty_db_path):
    cg.build_from_repo(empty_db_path, str(tiny_repo), "test/tiny")
    t = cg.analyze_topology(empty_db_path, repo="test/tiny", top_god=5)
    for key in ("god_nodes", "surprising_connections",
                 "orphans", "deep_inheritance_chains"):
        assert key in t


def test_triage_pr_scores_change(tiny_repo, empty_db_path):
    cg.build_from_repo(empty_db_path, str(tiny_repo), "test/tiny")
    r = cg.triage_pr(empty_db_path, ["a.py"], repo="test/tiny")
    assert "risk_score" in r
    assert "risk_tier" in r and r["risk_tier"] in ("LOW", "MEDIUM", "HIGH")
    assert "merge_order_hint" in r


def test_triage_empty_files_safe(empty_db_path, tmp_path):
    cg.build_from_repo(empty_db_path, str(tmp_path), "test/empty")
    r = cg.triage_pr(empty_db_path, [], repo="test/empty")
    assert r["risk_score"] == 0
    assert r["risk_tier"] == "LOW"


def test_stats_includes_buckets(tiny_repo, empty_db_path):
    cg.build_from_repo(empty_db_path, str(tiny_repo), "test/tiny")
    s = cg.stats(empty_db_path, repo="test/tiny")
    assert "edge_buckets" in s
    for k in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
        assert k in s["edge_buckets"]


def test_update_files_minor_vs_major(tiny_repo, empty_db_path):
    cg.build_from_repo(empty_db_path, str(tiny_repo), "test/tiny")
    # MINOR: edit helper body, no new symbols
    r = cg.update_files(empty_db_path, [{
        "path": "a.py", "text": "def helper(x):\n    return x + 99\n"
    }], repo="test/tiny")
    assert r["per_file"]["a.py"]["major"] is False

    # MAJOR: add a new function
    r = cg.update_files(empty_db_path, [{
        "path": "a.py",
        "text": "def helper(x):\n    return x + 1\n\ndef brand_new(y):\n    return y\n"
    }], repo="test/tiny")
    assert r["per_file"]["a.py"]["major"] is True
    assert "brand_new" in r["per_file"]["a.py"]["added"]
    # new symbol is queryable
    import sqlite3
    conn = sqlite3.connect(empty_db_path)
    n = conn.execute("SELECT COUNT(*) FROM symbols WHERE name='brand_new'").fetchone()[0]
    conn.close()
    assert n == 1


def test_semantic_stale_flag_and_clear(tiny_repo, empty_db_path):
    cg.build_from_repo(empty_db_path, str(tiny_repo), "test/tiny")
    cg.update_files(empty_db_path, [{
        "path": "a.py", "text": "def helper(x):\n    return x\n\ndef extra(): pass\n"
    }], repo="test/tiny")
    stale = cg.list_semantic_stale(empty_db_path, repo="test/tiny")
    assert any(s["path"] == "a.py" for s in stale)
    cleared = cg.clear_semantic_stale(empty_db_path, ["a.py"], repo="test/tiny")
    assert cleared >= 1
    assert cg.list_semantic_stale(empty_db_path, repo="test/tiny") == []
