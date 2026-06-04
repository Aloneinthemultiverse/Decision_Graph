"""AST extraction: imports, rationales, classes, param types."""
import pytest
from decisiongraph.codebase_ast import (
    parse_file_full, extract_text_from_any,
    supported_languages, supported_multimodal_exts)


def test_supported_languages_includes_core():
    langs = supported_languages()
    for required in ("python", "javascript", "typescript", "go", "java", "rust"):
        assert required in langs, f"missing language: {required}"


def test_supported_multimodal_exts():
    exts = supported_multimodal_exts()
    for required in (".pdf", ".puml", ".mmd", ".drawio"):
        assert required in exts


def test_python_parse_yields_imports_and_chunks():
    code = """
import os
from typing import Optional

class Foo:
    # WHY: just an example
    def bar(self, x: int) -> int:
        return x + 1
"""
    r = parse_file_full("test.py", code)
    assert r["lang"] == "python"
    assert len(r["imports"]) == 2
    assert any(i["local_name"] == "os" for i in r["imports"])
    assert any(i["target_name"] == "Optional" for i in r["imports"])
    assert len(r["chunks"]) >= 2     # Foo + bar
    rationales = r["rationales"]
    assert any(rt["tag"] == "WHY" for rt in rationales)


def test_rationales_across_tags():
    code = "\n".join([
        "# HACK: temporary",
        "# SAFETY: must be threadsafe",
        "# TODO: refactor",
        "def f(): pass",
    ])
    r = parse_file_full("test.py", code)
    tags = {x["tag"] for x in r["rationales"]}
    assert {"HACK", "SAFETY", "TODO"}.issubset(tags)


def test_class_bases_extracted():
    code = "class Sub(Base, Mixin):\n    pass\n"
    r = parse_file_full("test.py", code)
    classes = [c for c in r["chunks"] if c["kind"] == "class_definition"]
    assert classes
    bases = classes[0].get("bases") or []
    assert "Base" in bases and "Mixin" in bases


def test_extract_text_from_any_skips_unknown():
    assert extract_text_from_any("foo.unknown") is None


def test_extract_text_plantuml(tmp_path):
    p = tmp_path / "diag.puml"
    p.write_text("@startuml\nactor A\nA -> B: hi\n@enduml\n",
                 encoding="utf-8")
    r = extract_text_from_any(str(p))
    assert r["kind"] == "plantuml"
    assert "actor" in r["text"]
