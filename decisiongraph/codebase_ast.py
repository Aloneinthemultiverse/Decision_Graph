"""AST-aware codebase ingestion — tree-sitter chunking + hierarchical
summaries + call-graph edges + incremental cache.

Layered on top of `codebase.ingest_github_url`. When tree-sitter is available
and the file extension is supported, we extract function/class chunks
individually instead of summarising the whole file. Each chunk → one decision
node. Function-call edges → one graph triple per call.

Public API:
  • parse_repo_ast(repo_path, hash_cache)            → list of structured chunks
  • build_hierarchical_summaries(client, model, chunks, repo)
                                                       → dict per folder + repo
  • build_call_edges(chunks)                          → list[(src, dst)] triples
  • compute_file_hashes(repo_path)                    → {path: sha1}

Per-language support today: Python · JavaScript · TypeScript · Go.
Other languages fall back to file-level summary (the v0 path).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

# ── lazy tree-sitter wiring — never crash if a language isn't installed ─────
_LANG_CACHE: dict[str, object] = {}
_PARSER_CACHE: dict[str, object] = {}

# Multimodal-lite: file extensions we can extract text from even though
# tree-sitter can't parse them. Handled by `extract_text_from_any()`.
_MULTIMODAL_EXTS = {
    ".pdf":     "pdf",         # design docs, RFCs, architecture papers
    ".puml":    "plantuml",    # PlantUML architecture diagrams (text-based)
    ".plantuml":"plantuml",
    ".mmd":     "mermaid",     # Mermaid diagrams (text-based)
    ".mermaid": "mermaid",
    ".drawio":  "drawio",      # draw.io XML (we extract text labels)
    ".dot":     "graphviz",    # Graphviz dot files
    ".gv":      "graphviz",
}


_EXT_TO_LANG = {
    # Tier-1 (full support: function/class/call mappings)
    ".py":     "python",
    ".js":     "javascript",
    ".jsx":    "javascript",
    ".mjs":    "javascript",
    ".cjs":    "javascript",
    ".ts":     "typescript",
    ".tsx":    "tsx",
    ".go":     "go",
    ".rs":     "rust",
    ".java":   "java",
    ".c":      "c",
    ".h":      "c",
    ".cc":     "cpp",
    ".cpp":    "cpp",
    ".cxx":    "cpp",
    ".hpp":    "cpp",
    ".rb":     "ruby",
    ".php":    "php",
    ".cs":     "c_sharp",
    ".sh":     "bash",
    ".bash":   "bash",
    ".scala":  "scala",
    ".kt":     "kotlin",
    ".kts":    "kotlin",
    ".swift":  "swift",
    ".ex":     "elixir",
    ".exs":    "elixir",
    ".hs":     "haskell",
    ".ml":     "ocaml",
    ".mli":    "ocaml",
    ".jl":     "julia",
    ".pl":     "perl",
    ".pm":     "perl",
    ".zig":    "zig",
    ".lua":    "lua",
    ".sol":    "solidity",
    ".sql":    "sql",
    ".svelte": "svelte",
    # Tier-2 (parsed, but no extracted symbols — they're for completeness)
    ".html":   "html",
    ".css":    "css",
    ".json":   "json",
    ".yaml":   "yaml",
    ".yml":    "yaml",
    ".toml":   "toml",
    ".md":     "markdown",
}

# query patterns: each language tells us which AST node types we want as chunks
_FUNCTION_NODES = {
    "python":     {"function_definition", "class_definition"},
    "javascript": {"function_declaration", "method_definition",
                    "class_declaration", "arrow_function",
                    "function_expression", "generator_function_declaration"},
    "typescript": {"function_declaration", "method_definition",
                    "class_declaration", "interface_declaration",
                    "arrow_function", "function_expression"},
    "tsx":        {"function_declaration", "method_definition",
                    "class_declaration", "interface_declaration",
                    "arrow_function", "function_expression"},
    "go":         {"function_declaration", "method_declaration", "type_declaration"},
    "rust":       {"function_item", "struct_item", "impl_item",
                    "trait_item", "enum_item", "mod_item"},
    "java":       {"method_declaration", "class_declaration",
                    "interface_declaration", "enum_declaration",
                    "constructor_declaration"},
    "c":          {"function_definition", "struct_specifier",
                    "type_definition", "enum_specifier"},
    "cpp":        {"function_definition", "class_specifier",
                    "struct_specifier", "enum_specifier",
                    "namespace_definition", "template_declaration"},
    "ruby":       {"method", "singleton_method", "class", "module"},
    "php":        {"function_definition", "method_declaration",
                    "class_declaration", "interface_declaration",
                    "trait_declaration"},
    "c_sharp":    {"method_declaration", "class_declaration",
                    "interface_declaration", "struct_declaration",
                    "constructor_declaration", "property_declaration",
                    "namespace_declaration", "enum_declaration"},
    "bash":       {"function_definition"},
    "lua":        {"function_declaration", "function_definition",
                    "local_function"},
    "scala":      {"function_definition", "class_definition",
                    "trait_definition", "object_definition"},
    "kotlin":     {"function_declaration", "class_declaration",
                    "interface_declaration", "object_declaration",
                    "property_declaration"},
    "swift":      {"function_declaration", "class_declaration",
                    "struct_declaration", "protocol_declaration",
                    "extension_declaration", "init_declaration"},
    "elixir":     {"call"},   # def/defp/defmodule are call nodes in elixir grammar
    "haskell":    {"function", "signature", "data_type",
                    "type_synonym", "newtype"},
    "ocaml":      {"value_definition", "type_definition",
                    "module_definition", "class_definition"},
    "julia":      {"function_definition", "short_function_definition",
                    "macro_definition", "struct_definition"},
    "perl":       {"subroutine_declaration_statement",
                    "named_block_statement"},
    "zig":        {"function_declaration", "variable_declaration",
                    "struct_declaration"},
    "solidity":   {"function_definition", "contract_declaration",
                    "interface_declaration", "library_declaration",
                    "modifier_definition", "event_definition",
                    "struct_declaration"},
    "sql":        {"create_function_statement", "create_procedure_statement",
                    "create_table_statement", "create_view_statement"},
    "svelte":     {"function_declaration", "method_definition",
                    "class_declaration", "arrow_function"},
    # markdown/html/css/json/yaml/toml: no functions per se — skip extraction
    "markdown":   set(),
    "html":       set(),
    "css":        set(),
    "json":       set(),
    "yaml":       set(),
    "toml":       set(),
}

# call-expression nodes per language → for edges
_CALL_NODES = {
    "python":     {"call"},
    "javascript": {"call_expression"},
    "typescript": {"call_expression"},
    "tsx":        {"call_expression"},
    "go":         {"call_expression"},
    "rust":       {"call_expression", "macro_invocation"},
    "java":       {"method_invocation", "object_creation_expression"},
    "c":          {"call_expression"},
    "cpp":        {"call_expression"},
    "ruby":       {"call", "method_call"},
    "php":        {"function_call_expression", "member_call_expression",
                    "scoped_call_expression"},
    "c_sharp":    {"invocation_expression", "object_creation_expression"},
    "bash":       {"command"},
    "lua":        {"function_call"},
    "scala":      {"call_expression"},
    "kotlin":     {"call_expression"},
    "swift":      {"call_expression"},
    "elixir":     {"call"},
    "haskell":    {"function_application"},
    "ocaml":      {"application_expression"},
    "julia":      {"call_expression", "macro_expression"},
    "perl":       {"call_expression"},
    "zig":        {"call_expression"},
    "solidity":   {"call_expression"},
    "sql":        set(),   # SQL has no "calls" between statements
    "svelte":     {"call_expression"},
    "markdown":   set(), "html": set(), "css": set(),
    "json":       set(), "yaml": set(), "toml": set(),
}


# For most language packs the function is called `.language()`. A few use
# language_LANG() (typescript, ocaml, php). Map them out so the loader stays
# clean.
_LANG_LOADER = {
    "python":     ("tree_sitter_python",     "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx":        ("tree_sitter_typescript", "language_tsx"),
    "go":         ("tree_sitter_go",         "language"),
    "rust":       ("tree_sitter_rust",       "language"),
    "java":       ("tree_sitter_java",       "language"),
    "c":          ("tree_sitter_c",          "language"),
    "cpp":        ("tree_sitter_cpp",        "language"),
    "ruby":       ("tree_sitter_ruby",       "language"),
    "php":        ("tree_sitter_php",        "language_php"),
    "c_sharp":    ("tree_sitter_c_sharp",    "language"),
    "bash":       ("tree_sitter_bash",       "language"),
    "lua":        ("tree_sitter_lua",        "language"),
    "scala":      ("tree_sitter_scala",      "language"),
    "kotlin":     ("tree_sitter_kotlin",     "language"),
    "swift":      ("tree_sitter_swift",      "language"),
    "elixir":     ("tree_sitter_elixir",     "language"),
    "haskell":    ("tree_sitter_haskell",    "language"),
    "ocaml":      ("tree_sitter_ocaml",      "language_ocaml"),
    "julia":      ("tree_sitter_julia",      "language"),
    "perl":       ("tree_sitter_perl",       "language"),
    "zig":        ("tree_sitter_zig",        "language"),
    "solidity":   ("tree_sitter_solidity",   "language"),
    "sql":        ("tree_sitter_sql",        "language"),
    "svelte":     ("tree_sitter_svelte",     "language"),
    "html":       ("tree_sitter_html",       "language"),
    "css":        ("tree_sitter_css",        "language"),
    "json":       ("tree_sitter_json",       "language"),
    "yaml":       ("tree_sitter_yaml",       "language"),
    "toml":       ("tree_sitter_toml",       "language"),
    "markdown":   ("tree_sitter_markdown",   "language"),
}


def _get_language(lang_name: str):
    """Lazy-load tree-sitter language pack. Cached. Returns None if missing."""
    if lang_name in _LANG_CACHE:
        return _LANG_CACHE[lang_name]
    loader = _LANG_LOADER.get(lang_name)
    if not loader:
        _LANG_CACHE[lang_name] = None
        return None
    module_name, fn_name = loader
    try:
        import importlib
        from tree_sitter import Language
        m = importlib.import_module(module_name)
        fn = getattr(m, fn_name)
        lang = Language(fn())
        _LANG_CACHE[lang_name] = lang
        return lang
    except Exception:
        _LANG_CACHE[lang_name] = None
        return None


def supported_languages() -> list[str]:
    """Return list of language names the parser can handle right now."""
    return [k for k, (mod, fn) in _LANG_LOADER.items()
            if _get_language(k) is not None]


def _get_parser(lang_name: str):
    if lang_name in _PARSER_CACHE:
        return _PARSER_CACHE[lang_name]
    lang = _get_language(lang_name)
    if lang is None:
        _PARSER_CACHE[lang_name] = None
        return None
    try:
        from tree_sitter import Parser
        p = Parser(lang)
        _PARSER_CACHE[lang_name] = p
        return p
    except Exception:
        _PARSER_CACHE[lang_name] = None
        return None


def _name_of(node, src: bytes) -> str:
    """Best-effort extraction of the name for a function/class node."""
    for child in node.children:
        if child.type in ("identifier", "property_identifier",
                           "type_identifier", "field_identifier"):
            return src[child.start_byte:child.end_byte].decode(
                "utf-8", errors="replace")
    return "(anon)"


_CLASS_NODE_TYPES = {
    "python":     {"class_definition"},
    "javascript": {"class_declaration"},
    "typescript": {"class_declaration"},
    "tsx":        {"class_declaration"},
    "java":       {"class_declaration"},
    "ruby":       {"class"},
    "php":        {"class_declaration"},
    "c_sharp":    {"class_declaration"},
    "kotlin":     {"class_declaration"},
    "scala":      {"class_definition"},
    "swift":      {"class_declaration"},
}


def _walk_chunks(node, src: bytes, lang: str, path: str,
                  out: list[dict], parent: Optional[str] = None):
    """DFS-walk the AST emitting function/class/method chunks at any depth."""
    func_types = _FUNCTION_NODES.get(lang, set())
    class_types = _CLASS_NODE_TYPES.get(lang, set())
    if node.type in func_types:
        name = _name_of(node, src)
        text = src[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace")
        qualified = f"{parent}.{name}" if parent else name
        bases = _extract_bases(node, src, lang) if node.type in class_types else []
        # only extract param types for actual functions (not classes/interfaces)
        param_types = (_extract_param_types(node, src, lang)
                        if node.type not in class_types else {})
        out.append({
            "path":         path,
            "lang":         lang,
            "kind":         node.type,
            "name":         qualified,
            "start_line":   node.start_point[0] + 1,
            "end_line":     node.end_point[0] + 1,
            "code":         text,
            "calls":        [],
            "bases":        bases,
            "param_types":  param_types,
        })
        # walk children but mark this as the new parent (for nested classes/methods)
        for child in node.children:
            _walk_chunks(child, src, lang, path, out, parent=qualified)
        return
    for child in node.children:
        _walk_chunks(child, src, lang, path, out, parent=parent)


def _extract_calls_in_node(node, src: bytes, lang: str) -> list[str]:
    """Find all call expressions inside a node — return list of callee names.

    Legacy flat-name extractor kept for backward compat. New code uses
    _extract_calls_rich which also returns the receiver/prefix chain.
    """
    return [c["name"] for c in _extract_calls_rich(node, src, lang)]


_RATIONALE_TAGS = ("WHY", "HACK", "NOTE", "TODO", "FIXME", "XXX",
                    "SAFETY", "PERF", "BUG", "WARNING", "OPTIMIZE",
                    "REVIEW", "DEPRECATED")

# Each language's tree-sitter has a distinct comment node type.
_COMMENT_TYPES = {
    "python":     {"comment"},
    "javascript": {"comment"},
    "typescript": {"comment"},
    "tsx":        {"comment"},
    "go":         {"comment"},
    "rust":       {"line_comment", "block_comment"},
    "java":       {"line_comment", "block_comment"},
    "c":          {"comment"},
    "cpp":        {"comment"},
    "ruby":       {"comment"},
    "php":        {"comment"},
    "c_sharp":    {"comment"},
    "bash":       {"comment"},
    "lua":        {"comment"},
    "scala":      {"line_comment", "block_comment"},
    "kotlin":     {"line_comment", "multiline_comment"},
    "swift":      {"comment", "multiline_comment"},
    "elixir":     {"comment"},
    "haskell":    {"comment"},
    "ocaml":      {"comment"},
    "julia":      {"line_comment", "block_comment"},
    "perl":       {"comment"},
    "zig":        {"line_comment", "doc_comment"},
    "solidity":   {"comment"},
    "sql":        {"comment", "marginalia"},
    "svelte":     {"comment"},
}


def _extract_rationales(root_node, src: bytes, lang: str) -> list[dict]:
    """Walk comments looking for `# WHY: ...`, `// HACK: ...` etc.

    Returns: [{tag, text, line, end_line}, ...]"""
    types = _COMMENT_TYPES.get(lang, {"comment"})
    out: list[dict] = []

    def _strip_comment_prefix(s: str) -> str:
        s = s.strip()
        # python #, shell #
        if s.startswith("#"): s = s.lstrip("#").strip()
        # js/ts/go/rust //
        elif s.startswith("//"): s = s[2:].strip()
        # block /* */
        elif s.startswith("/*"):
            s = s[2:]
            if s.endswith("*/"): s = s[:-2]
            s = s.strip().lstrip("*").strip()
        # python docstring (rare in comment node)
        elif s.startswith('"""') or s.startswith("'''"):
            s = s.strip('"').strip("'").strip()
        # haskell, sql --
        elif s.startswith("--"): s = s[2:].strip()
        # html/svelte <!-- -->
        elif s.startswith("<!--"):
            s = s[4:]
            if s.endswith("-->"): s = s[:-3]
            s = s.strip()
        return s

    def _walk(n):
        if n.type in types:
            raw = src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")
            txt = _strip_comment_prefix(raw)
            # match TAG: rest  (case-insensitive)
            for tag in _RATIONALE_TAGS:
                up = txt.upper()
                if up.startswith(tag + ":") or up.startswith(tag + " "):
                    rest = txt[len(tag):].lstrip(": ").strip()
                    if rest:    # avoid pure-tag lines
                        out.append({
                            "tag":      tag,
                            "text":     rest[:500],
                            "line":     n.start_point[0] + 1,
                            "end_line": n.end_point[0] + 1,
                        })
                    break
        for c in n.children:
            _walk(c)

    _walk(root_node)
    return out


def _extract_param_types(func_node, src: bytes, lang: str) -> dict[str, str]:
    """For a Python/TS function definition, return {param_name → type_name}
    based on annotations. Used to seed receiver→class lookups so that
    `req.send()` in `def fn(req: Request)` can resolve to `Request.send`."""
    types: dict[str, str] = {}

    def _txt(n) -> str:
        return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

    if lang == "python":
        # Python: parameters → typed_parameter → identifier + type
        for child in func_node.children:
            if child.type == "parameters":
                for p in child.children:
                    if p.type == "typed_parameter":
                        name_node = None
                        type_node = None
                        for sub in p.children:
                            if sub.type == "identifier" and name_node is None:
                                name_node = sub
                            elif sub.type == "type":
                                type_node = sub
                        if name_node and type_node:
                            tname = _txt(type_node).strip()
                            # take last identifier of a complex type
                            # `Optional[Request]` → "Request"
                            for tok in reversed([t for t in tname
                                                  .replace("[", " ").replace("]", " ")
                                                  .replace(",", " ").split()]):
                                if tok and tok[0].isalpha():
                                    types[_txt(name_node)] = tok.split(".")[-1]
                                    break
                    elif p.type == "typed_default_parameter":
                        # `x: T = default`
                        name_node = p.child_by_field_name("name")
                        type_node = p.child_by_field_name("type")
                        if name_node and type_node:
                            tname = _txt(type_node).strip()
                            for tok in reversed([t for t in tname
                                                  .replace("[", " ").replace("]", " ")
                                                  .replace(",", " ").split()]):
                                if tok and tok[0].isalpha():
                                    types[_txt(name_node)] = tok.split(".")[-1]
                                    break
    elif lang in ("typescript", "tsx"):
        # TS: required_parameter / optional_parameter with type_annotation
        for child in func_node.children:
            if child.type in ("formal_parameters", "parameter_list"):
                for p in child.children:
                    if p.type in ("required_parameter", "optional_parameter"):
                        name_n = p.child_by_field_name("pattern")
                        type_n = p.child_by_field_name("type")
                        if name_n and type_n:
                            tname = _txt(type_n).lstrip(":").strip()
                            for tok in reversed([t for t in tname
                                                  .replace("[", " ").replace("]", " ")
                                                  .replace("|", " ").split()]):
                                if tok and tok[0].isalpha():
                                    types[_txt(name_n)] = tok.split(".")[-1]
                                    break
    return types


def _extract_calls_rich(node, src: bytes, lang: str) -> list[dict]:
    """Find all call expressions and return rich info per call:
       {name, receiver, line} where receiver is the leftmost identifier of an
       attribute chain (e.g. `foo` for `foo.bar.baz()`) or None for bare calls.

    This is what import-scope resolution needs: to know whether the callee
    is reached through an aliased module (`ctx.RequestContext()` where `ctx`
    is imported) vs a free identifier (`RequestContext()`)."""
    call_types = _CALL_NODES.get(lang, set())
    out: list[dict] = []

    def _flatten_attribute(attr_node) -> tuple[Optional[str], str]:
        """Walk an attribute chain to find (leftmost_receiver, leaf_name).
        For `a.b.c.d()` → ("a", "d"). For `self.foo()` → ("self", "foo")."""
        full = src[attr_node.start_byte:attr_node.end_byte].decode(
            "utf-8", errors="replace")
        parts = full.split(".")
        if len(parts) >= 2:
            return parts[0], parts[-1]
        return None, parts[0]

    def _walk(n):
        if n.type in call_types:
            callee_name = None
            receiver = None
            for child in n.children:
                if child.type in ("identifier", "field_identifier",
                                    "property_identifier"):
                    callee_name = src[child.start_byte:child.end_byte].decode(
                        "utf-8", errors="replace")
                    break
                elif child.type in ("attribute", "member_expression",
                                     "field_access", "selector_expression",
                                     "scoped_identifier"):
                    receiver, callee_name = _flatten_attribute(child)
                    break
            if callee_name:
                out.append({
                    "name":     callee_name,
                    "receiver": receiver,
                    "line":     n.start_point[0] + 1,
                })
        for c in n.children:
            _walk(c)

    _walk(node)
    return out


# ── per-file IMPORT extraction (the key Phase-1.1 piece) ──────────────────
def _extract_imports(root_node, src: bytes, lang: str) -> list[dict]:
    """Pull every import statement out of a file.

    Returns list of {local_name, target_module, target_name, kind} where:
      • local_name    = what this file calls the imported thing
      • target_module = the dotted module path being imported from
      • target_name   = original symbol name (None for whole-module imports)
      • kind          = 'module' | 'symbol' | 'wildcard'

    Per-language tree-sitter import node types differ; we cover the big six
    (python/js/ts/go/java/rust) since those are the benchmark languages."""
    imports: list[dict] = []

    def _txt(n) -> str:
        return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

    def _walk_python(n):
        # `import foo`, `import foo as bar`, `import a.b.c`
        if n.type == "import_statement":
            for child in n.children:
                if child.type == "dotted_name":
                    mod = _txt(child)
                    leaf = mod.split(".")[-1]
                    imports.append({"local_name": leaf, "target_module": mod,
                                    "target_name": None, "kind": "module"})
                elif child.type == "aliased_import":
                    mod_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if mod_node and alias_node:
                        imports.append({"local_name": _txt(alias_node),
                                        "target_module": _txt(mod_node),
                                        "target_name": None, "kind": "module"})
            return    # don't recurse into already-handled import
        # `from foo import bar`, `from foo import bar as baz`, `from foo import *`
        if n.type == "import_from_statement":
            mod_node = n.child_by_field_name("module_name")
            mod_path = _txt(mod_node) if mod_node else ""
            mod_span = (mod_node.start_byte, mod_node.end_byte) if mod_node else None
            for child in n.children:
                # skip the module-name child (compare by byte-span, not identity)
                if mod_span and (child.start_byte, child.end_byte) == mod_span:
                    continue
                if child.type == "dotted_name":
                    leaf = _txt(child).split(".")[-1]
                    imports.append({"local_name": leaf, "target_module": mod_path,
                                    "target_name": leaf, "kind": "symbol"})
                elif child.type == "aliased_import":
                    nm = child.child_by_field_name("name")
                    al = child.child_by_field_name("alias")
                    if nm and al:
                        imports.append({"local_name": _txt(al),
                                        "target_module": mod_path,
                                        "target_name": _txt(nm).split(".")[-1],
                                        "kind": "symbol"})
                elif child.type == "wildcard_import":
                    imports.append({"local_name": "*", "target_module": mod_path,
                                    "target_name": None, "kind": "wildcard"})
            return    # don't recurse into already-handled import
        for c in n.children:
            _walk_python(c)

    def _walk_js(n):
        # `import { Foo, Bar as Baz } from 'mod'`, `import Foo from 'mod'`,
        # `import * as X from 'mod'`, `const Foo = require('mod')`
        if n.type == "import_statement":
            mod = ""
            for child in n.children:
                if child.type == "string":
                    mod = _txt(child).strip("'\"`")
            for child in n.children:
                if child.type == "import_clause":
                    for sub in child.children:
                        if sub.type == "identifier":
                            imports.append({"local_name": _txt(sub),
                                            "target_module": mod,
                                            "target_name": "default",
                                            "kind": "symbol"})
                        elif sub.type == "named_imports":
                            for spec in sub.children:
                                if spec.type == "import_specifier":
                                    name_n = spec.child_by_field_name("name")
                                    alias_n = spec.child_by_field_name("alias")
                                    name = _txt(name_n) if name_n else ""
                                    local = _txt(alias_n) if alias_n else name
                                    if name and local:
                                        imports.append({"local_name": local,
                                                        "target_module": mod,
                                                        "target_name": name,
                                                        "kind": "symbol"})
                        elif sub.type == "namespace_import":
                            for s2 in sub.children:
                                if s2.type == "identifier":
                                    imports.append({"local_name": _txt(s2),
                                                    "target_module": mod,
                                                    "target_name": None,
                                                    "kind": "module"})
        # `const x = require('y')`
        elif n.type == "variable_declarator":
            id_n = n.child_by_field_name("name")
            val_n = n.child_by_field_name("value")
            if id_n and val_n and val_n.type == "call_expression":
                fn_n = val_n.child_by_field_name("function")
                args_n = val_n.child_by_field_name("arguments")
                if fn_n and _txt(fn_n) == "require" and args_n:
                    for a in args_n.children:
                        if a.type == "string":
                            mod = _txt(a).strip("'\"`")
                            imports.append({"local_name": _txt(id_n),
                                            "target_module": mod,
                                            "target_name": None,
                                            "kind": "module"})
        for c in n.children:
            _walk_js(c)

    def _walk_go(n):
        if n.type == "import_declaration":
            # walk all import_spec entries
            def _spec(s):
                pkg_n = s.child_by_field_name("path")
                alias_n = s.child_by_field_name("name")
                if pkg_n:
                    mod = _txt(pkg_n).strip('"')
                    leaf = mod.rsplit("/", 1)[-1]
                    local = _txt(alias_n) if alias_n else leaf
                    imports.append({"local_name": local, "target_module": mod,
                                    "target_name": None, "kind": "module"})
            for child in n.children:
                if child.type == "import_spec":
                    _spec(child)
                elif child.type == "import_spec_list":
                    for s in child.children:
                        if s.type == "import_spec":
                            _spec(s)
        for c in n.children:
            _walk_go(c)

    def _walk_java(n):
        if n.type == "import_declaration":
            for child in n.children:
                if child.type == "scoped_identifier":
                    full = _txt(child)
                    leaf = full.rsplit(".", 1)[-1]
                    mod = full.rsplit(".", 1)[0] if "." in full else ""
                    imports.append({"local_name": leaf, "target_module": mod,
                                    "target_name": leaf, "kind": "symbol"})
        for c in n.children:
            _walk_java(c)

    if lang == "python":
        _walk_python(root_node)
    elif lang in ("javascript", "typescript", "tsx"):
        _walk_js(root_node)
    elif lang == "go":
        _walk_go(root_node)
    elif lang == "java":
        _walk_java(root_node)
    # other languages: import resolution falls back to leaf-name only
    return imports


# ── per-class BASE extraction (Phase 1.4) ───────────────────────────────────
def _extract_bases(class_node, src: bytes, lang: str) -> list[str]:
    """For a class definition node, return the list of base-class names."""

    def _txt(n) -> str:
        return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

    bases: list[str] = []

    if lang == "python":
        # python: class Foo(Bar, Baz):  → arguments node holds bases
        for child in class_node.children:
            if child.type == "argument_list":
                for arg in child.children:
                    if arg.type in ("identifier", "attribute",
                                     "dotted_name"):
                        # take leaf of dotted path  module.Class → Class
                        bases.append(_txt(arg).split(".")[-1])
    elif lang in ("javascript", "typescript", "tsx"):
        # class Foo extends Bar {...} — class_heritage contains identifier
        for child in class_node.children:
            if child.type == "class_heritage":
                for sub in child.children:
                    if sub.type in ("identifier", "member_expression",
                                     "extends_clause"):
                        text = _txt(sub)
                        # strip 'extends '
                        text = text.replace("extends", "").strip()
                        if text:
                            bases.append(text.split(".")[-1])
    elif lang == "java":
        # class Foo extends Bar implements Baz, Qux — superclass + super_interfaces
        for child in class_node.children:
            if child.type == "superclass":
                for sub in child.children:
                    if sub.type in ("type_identifier", "scoped_type_identifier"):
                        bases.append(_txt(sub).split(".")[-1])
            elif child.type == "super_interfaces":
                for sub in child.children:
                    if sub.type == "type_list":
                        for t in sub.children:
                            if t.type in ("type_identifier", "scoped_type_identifier"):
                                bases.append(_txt(t).split(".")[-1])
    # ruby/php/c++ etc could be added later
    return bases


def parse_file_ast(file_path: str, code: str) -> list[dict]:
    """Parse one source file. Returns AST-chunked items or [] if unsupported.

    Each chunk:  {path, lang, kind, name, start_line, end_line, code, calls}
    Legacy entry-point. New callers should use parse_file_full() which also
    returns imports and rich call info for scope-aware resolution.
    """
    full = parse_file_full(file_path, code)
    return full["chunks"]


def parse_file_full(file_path: str, code: str) -> dict:
    """Parse one source file and return everything needed for scope-aware
    call resolution.

    Returns:
      {
        "lang":    str | None,
        "chunks":  [chunk_dict, ...]   # each has rich `calls` list now too
        "imports": [import_dict, ...]
      }

    Each chunk now carries `bases` (list of base-class names) and `calls`
    is a list of dicts {name, receiver, line} instead of flat strings.

    Returns {} keys empty if file isn't a supported language.
    """
    out = {"lang": None, "chunks": [], "imports": [], "rationales": []}
    ext = os.path.splitext(file_path)[1].lower()
    lang = _EXT_TO_LANG.get(ext)
    if not lang:
        return out
    parser = _get_parser(lang)
    if not parser:
        return out
    try:
        src_bytes = code.encode("utf-8", errors="replace")
        tree = parser.parse(src_bytes)
    except Exception:
        return out

    out["lang"] = lang

    # ── imports (file-level) ─────────────────────────────────────────────
    out["imports"] = _extract_imports(tree.root_node, src_bytes, lang)

    # ── rationales: # WHY:, // HACK:, etc. ───────────────────────────────
    out["rationales"] = _extract_rationales(tree.root_node, src_bytes, lang)

    # ── chunks + rich calls (scoped under each function/class) ───────────
    chunks: list[dict] = []
    _walk_chunks(tree.root_node, src_bytes, lang, file_path, chunks)
    for ch in chunks:
        try:
            sub_tree = parser.parse(ch["code"].encode("utf-8", errors="replace"))
            ch["calls"] = _extract_calls_rich(
                sub_tree.root_node, ch["code"].encode("utf-8"), lang)
        except Exception:
            ch["calls"] = []
    out["chunks"] = chunks
    return out


# ── hierarchical summaries ────────────────────────────────────────────────────
def summarise_chunk(client, model: str, ch: dict) -> str:
    """One-shot summary of a function/class chunk."""
    prompt = (
        f"Write ONE sentence (max 25 words) describing what the "
        f"`{ch['name']}` {ch['kind']} in `{ch['path']}` does. "
        f"Output the sentence directly — no preamble, no quotes, no headings, "
        f"no 'Summary:' label.\n\n"
        f"CODE:\n{ch['code'][:4000]}\n\nONE-SENTENCE DESCRIPTION:")
    try:
        r = client.messages.create(
            model=model, max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in r.content).strip()
        import re as _re
        text = _re.sub(r"^(SUMMARY:|DESCRIPTION:|ONE-SENTENCE.*?:)\s*", "", text, flags=_re.I)
        return text.strip().strip('"').strip("'")
    except Exception as e:
        return f"(summary failed: {e})"


def summarise_folder(client, model: str, folder_path: str,
                      child_summaries: list[str]) -> str:
    """Roll up child file/folder summaries into one folder-level summary."""
    if not child_summaries:
        return ""
    body = "\n".join(f"- {s}" for s in child_summaries[:30])
    prompt = (
        f"Write 2-3 sentences describing the role of the `{folder_path}` "
        f"folder. Use ONLY the file summaries below as source. "
        f"Output the sentences directly — no preamble, no headings, no labels.\n\n"
        f"FILES:\n{body}\n\nDESCRIPTION:")
    try:
        r = client.messages.create(
            model=model, max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in r.content).strip()
        import re as _re
        text = _re.sub(r"^(FOLDER SUMMARY:|SUMMARY:|DESCRIPTION:)\s*", "", text, flags=_re.I)
        return text.strip().strip('"').strip("'")
    except Exception as e:
        return f"(folder summary failed: {e})"


def summarise_repo(client, model: str, repo_name: str,
                    folder_summaries: dict[str, str]) -> str:
    """Roll up all folder-level summaries into a one-paragraph repo summary."""
    if not folder_summaries:
        return ""
    body = "\n".join(f"- {p}: {s}"
                      for p, s in list(folder_summaries.items())[:30])
    prompt = (
        f"Write a single paragraph (4-7 sentences) describing what the "
        f"`{repo_name}` repository is, what it does, and its overall "
        f"architecture. Use ONLY the folder summaries below as your source. "
        f"Output the paragraph directly — no preamble, no headings, no lists, "
        f"no instructions, no quotation marks.\n\n"
        f"FOLDER SUMMARIES:\n{body}\n\n"
        f"PARAGRAPH:")
    try:
        r = client.messages.create(
            model=model, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in r.content).strip()
        # strip common leaks ("PARAGRAPH:", numbered headings, "Step 1:", etc.)
        import re as _re
        text = _re.sub(r"^(PARAGRAPH:|REPO SUMMARY:|Summary:)\s*", "", text, flags=_re.I)
        text = _re.sub(r"^\d+\.\s*[*_]+[A-Z][^*_]*[*_]+:?\s*", "", text)
        return text.strip()
    except Exception as e:
        return f"(repo summary failed: {e})"


# ── call-graph edges ──────────────────────────────────────────────────────────
def build_call_edges(chunks: list[dict]) -> list[dict]:
    """From a list of AST chunks, build call-graph edges.

    Returns list of {src, dst, src_path, dst_path?} dicts. Edges are
    name-matched best-effort (cross-file resolution is heuristic — we look up
    callee name across all known chunk names; ambiguity is allowed).
    """
    by_name: dict[str, list[dict]] = {}
    for ch in chunks:
        # store both qualified and short names
        short = ch["name"].split(".")[-1]
        by_name.setdefault(short, []).append(ch)
        by_name.setdefault(ch["name"], []).append(ch)

    edges: list[dict] = []
    for ch in chunks:
        for callee in ch.get("calls", []):
            if callee in by_name and by_name[callee]:
                target = by_name[callee][0]   # take first match
                edges.append({
                    "src":      ch["name"],
                    "dst":      target["name"],
                    "src_path": ch["path"],
                    "dst_path": target["path"],
                })
    return edges


# ── incremental hashing ───────────────────────────────────────────────────────
def compute_file_hash(file_path: Path) -> str:
    """SHA-1 of file bytes — cheap, deterministic, fits in DG metadata."""
    h = hashlib.sha1()
    try:
        with open(file_path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return ""


def extract_text_from_any(file_path: str | Path) -> Optional[dict]:
    """Multimodal-lite: pull readable text from PDFs and architecture
    diagrams (PlantUML/Mermaid/draw.io/graphviz). Returns:
       {kind, text, label_count}  or  None if unsupported / fails.

    Used during ingest to feed `docs/architecture.pdf` and `docs/*.puml`
    into the semantic blueprint as decision nodes."""
    p = Path(file_path)
    ext = p.suffix.lower()
    kind = _MULTIMODAL_EXTS.get(ext)
    if not kind: return None

    try:
        if kind == "pdf":
            # pypdf is pure-Python, no system deps; cheapest option.
            try:
                from pypdf import PdfReader
            except ImportError:
                return {"kind": "pdf", "text": "",
                        "error": "pypdf not installed"}
            reader = PdfReader(str(p))
            pages = []
            for i, page in enumerate(reader.pages[:40]):  # cap at 40 pages
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    pages.append("")
            text = "\n\n".join(pages).strip()
            return {"kind": "pdf", "text": text[:120_000],
                    "page_count": len(reader.pages)}

        if kind in ("plantuml", "mermaid", "graphviz"):
            # text-based diagrams — just read the file
            text = p.read_text(encoding="utf-8", errors="replace")
            return {"kind": kind, "text": text[:80_000]}

        if kind == "drawio":
            # draw.io files are XML; pull every `value="..."` (node labels)
            import xml.etree.ElementTree as ET, re
            content = p.read_text(encoding="utf-8", errors="replace")
            labels = re.findall(r'value="([^"]+)"', content)
            text = " | ".join(labels[:500])
            return {"kind": "drawio", "text": text[:60_000],
                    "label_count": len(labels)}
    except Exception as e:
        return {"kind": kind, "text": "", "error": str(e)}
    return None


def supported_multimodal_exts() -> list[str]:
    """Return all extensions we handle via the multimodal-lite pipeline."""
    return sorted(_MULTIMODAL_EXTS.keys())


def diff_against_cache(current_hashes: dict[str, str],
                        cached_hashes: dict[str, str]) -> dict[str, list[str]]:
    """Compare two hash maps. Returns {added, changed, removed}."""
    cur = set(current_hashes.keys())
    cached = set(cached_hashes.keys())
    added = sorted(cur - cached)
    removed = sorted(cached - cur)
    changed = sorted(
        p for p in (cur & cached) if current_hashes[p] != cached_hashes[p])
    return {"added": added, "changed": changed, "removed": removed}
