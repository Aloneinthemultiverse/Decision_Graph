"""Phase 2 CAG: tiered blueprint slicer + KERNEL-CAG scratchpad."""
import os
import pytest
from decisiongraph.context_pack import get_context_pack, _split_blueprint
from decisiongraph.kernel_cag import KernelCAG


_BLUEPRINT = """# Project: acme/widget

## Mission

Widget does things. This is the small always-on overview.

## Key Concepts

- The project implements **frobnication**.


# Files in acme/widget

## File: src/api/auth.py

The file `src/api/auth.py` lives in folder `src/api/`. Handles auth.
It defines: `login`, `logout`.

## File: src/api/users.py

The file `src/api/users.py` lives in folder `src/api/`. User CRUD.

## File: src/db/models.py

The file `src/db/models.py` lives in folder `src/db/`. ORM models.

# Recent activity in acme/widget

- alice committed: fix auth bug
"""


@pytest.fixture
def blueprint_file(tmp_path):
    p = tmp_path / "acme_widget.md"
    p.write_text(_BLUEPRINT, encoding="utf-8")
    return str(p)


def test_split_separates_overview_files_tail():
    ov, secs, tail = _split_blueprint(_BLUEPRINT)
    assert "Mission" in ov and "frobnication" in ov
    assert "## File:" not in ov                      # overview stops before files
    assert set(secs) == {"src/api/auth.py", "src/api/users.py", "src/db/models.py"}
    assert "Recent activity" in tail                 # trailing top-level section
    assert "Recent activity" not in secs["src/db/models.py"]  # tail not glued to last file


def test_overview_only_when_no_task_files(blueprint_file):
    r = get_context_pack(blueprint_file, [])
    assert r["included_files"] == []
    assert r["total_files"] == 3
    assert "Mission" in r["markdown"]
    assert "## File:" not in r["markdown"]


def test_exact_file_plus_folder_siblings(blueprint_file):
    r = get_context_pack(blueprint_file, ["src/api/auth.py"])
    # auth.py + its folder sibling users.py, but NOT the db/ file
    assert "src/api/auth.py" in r["included_files"]
    assert "src/api/users.py" in r["included_files"]
    assert "src/db/models.py" not in r["included_files"]
    assert "Mission" in r["markdown"]                # overview always present


def test_no_siblings_when_disabled(blueprint_file):
    r = get_context_pack(blueprint_file, ["src/api/auth.py"],
                         include_folder_siblings=False)
    assert r["included_files"] == ["src/api/auth.py"]


def test_budget_truncates(blueprint_file):
    # budget clamps up to the overview size, so no file section can fit
    r = get_context_pack(blueprint_file, ["src/api/auth.py"], token_budget=1)
    assert r["truncated"] is True
    assert r["included_files"] == []                 # only overview fits


def test_unknown_file_reported_skipped(blueprint_file):
    r = get_context_pack(blueprint_file, ["does/not/exist.py"])
    assert r["skipped_files"] == ["does/not/exist.py"]
    assert r["included_files"] == []


def test_missing_blueprint_returns_error(tmp_path):
    r = get_context_pack(str(tmp_path / "nope.md"), [])
    assert "error" in r


# ── KERNEL-CAG ──────────────────────────────────────────────────────────────

def test_kernel_cag_load_slice_and_activity(tmp_path, blueprint_file):
    k = KernelCAG(str(tmp_path))
    pack = get_context_pack(blueprint_file, ["src/api/auth.py"])
    k.load_context_slice(pack)
    assert k.get_context_slice()["included_files"] == pack["included_files"]

    k.append("edit", {"path": "src/api/auth.py", "summary": "add refresh"})
    k.append("decision", {"question": "q", "answer": "a"})
    assert len(k.recent()) == 2
    assert len(k.recent(kind="edit")) == 1


def test_kernel_cag_rejects_bad_kind(tmp_path):
    k = KernelCAG(str(tmp_path))
    with pytest.raises(ValueError):
        k.append("bogus", {})


def test_kernel_cag_digest_and_persistence(tmp_path):
    k = KernelCAG(str(tmp_path))
    sid = k.session_id
    k.append("edit", {"path": "a.py"})
    k.append("edit", {"path": "a.py"})
    k.append("decision", {"question": "x", "answer": "y"})
    d = k.close()
    assert d["edit_count"] == 2
    assert d["edited_files"] == ["a.py"]
    assert d["decision_count"] == 1

    # reopen same session id -> entries persisted, ended set
    k2 = KernelCAG(str(tmp_path), session_id=sid)
    assert len(k2.recent(n=100)) == 3
    assert k2._data["ended"] is not None
    k2.discard()
    assert not os.path.exists(k2.path)
