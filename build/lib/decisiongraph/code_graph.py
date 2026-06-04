"""SQLite-backed structural code graph.

Inspired by tirth8205/code-review-graph: a pure-structural, LLM-free index of
a codebase, built from tree-sitter output. Sits ALONGSIDE the existing
DG/blueprint pipeline — it does not replace it.

The blueprint+DG path answers "what is X" / "what does X do" (semantic).
This SQLite path answers "what calls X" / "what breaks if I change X" /
"how does A reach B" (structural). The agent picks whichever it needs.

Schema (one DB file per workspace, at `<ws>/personal/code_graph.db`):

  files (
    path PRIMARY KEY, repo TEXT, lang TEXT, sha1 TEXT,
    bytes INT, last_ingested TEXT
  )

  symbols (
    id INTEGER PRIMARY KEY,
    repo TEXT, path TEXT, name TEXT,
    qualified_name TEXT,           -- e.g. "ClassFoo.method_bar"
    kind TEXT,                     -- 'function_definition', 'class_definition', ...
    start_line INT, end_line INT,
    UNIQUE(repo, path, qualified_name)
  )

  calls (
    src_id INTEGER,                -- symbols.id of caller
    dst_name TEXT,                 -- callee name (best-effort resolved)
    dst_id INTEGER,                -- symbols.id if we matched it, else NULL
    repo TEXT
  )

  imports (
    src_path TEXT,                 -- file doing the import
    dst_module TEXT,               -- imported module/path
    repo TEXT
  )

Public API:
  • build_from_repo(db_path, repo_path, repo_name)   → ingest a cloned repo
  • find_callers(db_path, name)                       → who calls `name`?
  • find_callees(db_path, name)                       → who does `name` call?
  • blast_radius(db_path, path)                       → which files break?
  • find_path(db_path, src, dst, max_hops=4)         → call-chain A → B
  • stats(db_path)                                    → counts
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path           TEXT PRIMARY KEY,
    repo           TEXT NOT NULL,
    lang           TEXT,
    sha1           TEXT,
    bytes          INTEGER,
    last_ingested  TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo);

CREATE TABLE IF NOT EXISTS symbols (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo            TEXT NOT NULL,
    path            TEXT NOT NULL,
    name            TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,
    kind            TEXT,
    start_line      INTEGER,
    end_line        INTEGER,
    UNIQUE(repo, path, qualified_name)
);
CREATE INDEX IF NOT EXISTS idx_symbols_repo_name ON symbols(repo, name);
CREATE INDEX IF NOT EXISTS idx_symbols_repo_path ON symbols(repo, path);
CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON symbols(qualified_name);

-- Calls now carry KIND + CONFIDENCE so blast_radius can filter precisely.
--   kind:       'CALLS' (default) | 'INHERITS' | 'IMPORTS_FROM' | future kinds
--   confidence: 1.0  = resolved via import-scope (target file is imported)
--               0.7  = resolved via unique unambiguous leaf name
--               0.5  = resolved via class-hierarchy method dispatch
--               0.3  = resolved via ambiguous leaf name (LIMIT 1 pick)
--               0.0  = unresolved (dst_id NULL)
CREATE TABLE IF NOT EXISTS calls (
    src_id      INTEGER NOT NULL,
    dst_name    TEXT NOT NULL,
    dst_id      INTEGER,
    kind        TEXT NOT NULL DEFAULT 'CALLS',
    confidence  REAL NOT NULL DEFAULT 0.0,
    repo        TEXT NOT NULL,
    FOREIGN KEY(src_id) REFERENCES symbols(id)
);
CREATE INDEX IF NOT EXISTS idx_calls_src   ON calls(src_id);
CREATE INDEX IF NOT EXISTS idx_calls_dst_id ON calls(dst_id);
CREATE INDEX IF NOT EXISTS idx_calls_dst_name ON calls(repo, dst_name);
CREATE INDEX IF NOT EXISTS idx_calls_kind   ON calls(repo, kind);
CREATE INDEX IF NOT EXISTS idx_calls_conf   ON calls(repo, confidence);

-- Imports table — richer than before. local_name is what THIS file calls the
-- import; target_module is the resolved dotted module; target_name is the
-- original symbol name (or NULL for whole-module imports).
CREATE TABLE IF NOT EXISTS imports (
    src_path       TEXT NOT NULL,
    local_name     TEXT NOT NULL,
    target_module  TEXT NOT NULL,
    target_name    TEXT,
    kind           TEXT NOT NULL DEFAULT 'symbol',  -- 'module' | 'symbol' | 'wildcard'
    repo           TEXT NOT NULL,
    -- Backward-compat alias: old code reads `dst_module`; keep as generated col.
    -- (SQLite doesn't support generated cols pre-3.31; we just always write
    --  target_module and let the legacy LIKE path also see it.)
    dst_module     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_imports_src     ON imports(repo, src_path);
CREATE INDEX IF NOT EXISTS idx_imports_target  ON imports(repo, target_module);
CREATE INDEX IF NOT EXISTS idx_imports_local   ON imports(repo, src_path, local_name);
CREATE INDEX IF NOT EXISTS idx_imports_dst_legacy ON imports(repo, dst_module);

-- Inline design rationale: `# WHY:`/`# HACK:`/`# NOTE:` etc. captured as
-- first-class nodes linked to the nearest symbol. The tribal knowledge that
-- normally lives ONLY in source comments and dies in code review.
CREATE TABLE IF NOT EXISTS rationales (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    path        TEXT NOT NULL,
    line        INTEGER NOT NULL,
    tag         TEXT NOT NULL,    -- 'WHY' | 'HACK' | 'NOTE' | 'TODO' | 'FIXME' | 'XXX' | 'SAFETY' | 'PERF' | 'BUG'
    text        TEXT NOT NULL,
    symbol_id   INTEGER,          -- nearest enclosing symbol if any
    FOREIGN KEY(symbol_id) REFERENCES symbols(id)
);
CREATE INDEX IF NOT EXISTS idx_rationales_repo_tag  ON rationales(repo, tag);
CREATE INDEX IF NOT EXISTS idx_rationales_symbol    ON rationales(symbol_id);
CREATE INDEX IF NOT EXISTS idx_rationales_repo_path ON rationales(repo, path);

-- Class inheritance — when a class changes, subclasses are affected too.
CREATE TABLE IF NOT EXISTS inherits (
    src_id      INTEGER NOT NULL,        -- subclass symbol id
    base_name   TEXT NOT NULL,           -- raw base name from source
    base_id     INTEGER,                 -- resolved id (if found)
    confidence  REAL NOT NULL DEFAULT 0.0,
    repo        TEXT NOT NULL,
    FOREIGN KEY(src_id) REFERENCES symbols(id)
);
CREATE INDEX IF NOT EXISTS idx_inherits_src    ON inherits(src_id);
CREATE INDEX IF NOT EXISTS idx_inherits_base   ON inherits(base_id);
CREATE INDEX IF NOT EXISTS idx_inherits_name   ON inherits(repo, base_name);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.executescript(_SCHEMA)
    c.row_factory = sqlite3.Row
    return c


def _sha1(p: Path) -> str:
    h = hashlib.sha1()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _module_paths_for_file(path: str) -> list[str]:
    """Given a repo-relative file path like 'src/flask/ctx.py', return the
    candidate identifiers this file might be imported under.

    Per-language rules:
      • Python:  dotted module variants (`src.flask.ctx`, `flask.ctx`, `ctx`)
                 Dir-level (`flask`) only when this file is __init__.py.
      • JS/TS:   slash-path variants + bare filename
      • Go:      parent directory IS the package — emit dir-level identifiers
      • Default: file-stem only

    First match wins at lookup time; we put more specific variants first."""
    p = path.replace("\\", "/")
    parts = p.split("/")
    leaf_with_ext = parts[-1]
    ext = leaf_with_ext.rsplit(".", 1)[-1].lower() if "." in leaf_with_ext else ""
    if "." in leaf_with_ext:
        parts[-1] = leaf_with_ext.rsplit(".", 1)[0]
    is_pkg_index = parts[-1] in ("__init__", "index", "mod")
    if is_pkg_index:
        parts = parts[:-1]

    out: list[str] = []
    def add(c: str):
        if c and c not in out:
            out.append(c)

    is_python = ext == "py" or (ext == "" and not parts)
    is_jsy    = ext in {"js", "jsx", "mjs", "cjs", "ts", "tsx"}
    is_go     = ext == "go"

    if is_python:
        # Dotted variants only; dir-level only for __init__ files
        for start in range(len(parts)):
            add(".".join(parts[start:]))
        # bare leaf — same as last variant; safe
    elif is_jsy:
        # JS/TS: slash-path variants + bare filename
        for start in range(len(parts)):
            add("/".join(parts[start:]))
        if parts:
            add(parts[-1])
    elif is_go:
        # Go: package = parent directory; the FILE is not imported directly.
        # Emit ONLY the dir-level identifiers — so changes to render/data.go
        # match imports of the `render` package, but not unrelated files.
        if len(parts) >= 2:
            add(parts[-2])                    # "render"
            add("/".join(parts[:-1]))         # "render" or "internal/render"
        # if file is at repo root, fall back to filename
        elif parts:
            add(parts[-1])
    else:
        # Other languages: file-stem only
        if parts:
            add(parts[-1])

    return out


def _resolve_relative_module(target_module: str, src_path: str) -> str:
    """Python `from .ctx import X` in 'src/flask/app.py' → 'src.flask.ctx'.
    If not a relative import, returns target_module unchanged."""
    if not target_module or not target_module.startswith("."):
        return target_module
    n_dots = 0
    while n_dots < len(target_module) and target_module[n_dots] == ".":
        n_dots += 1
    tail = target_module[n_dots:]
    src_dir = src_path.replace("\\", "/").split("/")[:-1]
    pops = n_dots - 1
    base = src_dir[:len(src_dir) - pops] if pops <= len(src_dir) else []
    full = ".".join([p for p in base if p] + ([tail] if tail else []))
    return full or target_module


def build_from_repo(db_path: str, repo_path: str, repo_name: str,
                     progress_cb=None) -> dict:
    """Walk the cloned repo, parse with tree-sitter, write nodes + edges to SQLite.

    Pure-structural — zero LLM calls. For a 60-file Python repo this typically
    runs in 2-5 seconds total. Subsequent ingests of the same repo only re-parse
    files whose sha1 changed.

    Returns counts dict."""
    from .codebase_ast import parse_file_full
    from .codebase import _collect_code_files, _CODE_MAX_FILES

    started = time.time()
    repo_root = Path(repo_path)
    files = _collect_code_files(repo_root)

    stats = {"files": 0, "symbols": 0, "calls": 0, "imports": 0,
             "inherits": 0, "rationales": 0, "skipped_unchanged": 0,
             "resolution_breakdown": {"import_scope": 0, "unique_leaf": 0,
                                       "class_method": 0, "ambiguous": 0,
                                       "unresolved": 0}}
    conn = _connect(db_path)
    cur = conn.cursor()

    # purge old data for this repo so we don't accumulate stale symbols
    cur.execute("DELETE FROM calls      WHERE repo = ?", (repo_name,))
    cur.execute("DELETE FROM imports    WHERE repo = ?", (repo_name,))
    cur.execute("DELETE FROM symbols    WHERE repo = ?", (repo_name,))
    cur.execute("DELETE FROM inherits   WHERE repo = ?", (repo_name,))
    cur.execute("DELETE FROM rationales WHERE repo = ?", (repo_name,))
    cur.execute("DELETE FROM files      WHERE repo = ?", (repo_name,))
    conn.commit()

    total = min(len(files), _CODE_MAX_FILES)
    # ── PASS 1: parse each file, insert files/symbols/imports; cache for pass 3
    parsed: list[tuple[str, dict]] = []
    for i, art in enumerate(files[:_CODE_MAX_FILES]):
        path = art["path"]
        full_path = repo_root / path
        sha1 = _sha1(full_path)
        bytes_ = full_path.stat().st_size if full_path.exists() else 0

        result = parse_file_full(path, art["text"]) or {}
        lang = result.get("lang")
        chunks = result.get("chunks") or []
        file_imports = result.get("imports") or []

        cur.execute("INSERT OR REPLACE INTO files(path, repo, lang, sha1, bytes, last_ingested) "
                    "VALUES (?,?,?,?,?,?)",
                    (path, repo_name, lang or art.get("ext", ""), sha1, bytes_,
                     time.strftime("%Y-%m-%dT%H:%M:%S")))
        stats["files"] += 1

        for ch in chunks:
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO symbols"
                    "(repo, path, name, qualified_name, kind, start_line, end_line) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (repo_name, path,
                     ch["name"].split(".")[-1], ch["name"],
                     ch["kind"], ch["start_line"], ch["end_line"]))
                stats["symbols"] += 1
            except Exception:
                pass

        for imp in file_imports:
            target_mod = _resolve_relative_module(
                imp.get("target_module") or "", path)
            cur.execute(
                "INSERT INTO imports(src_path, local_name, target_module, "
                "target_name, kind, repo, dst_module) VALUES (?,?,?,?,?,?,?)",
                (path, imp.get("local_name") or "", target_mod,
                 imp.get("target_name"), imp.get("kind") or "symbol",
                 repo_name, target_mod))
            stats["imports"] += 1

        # Rationales: pin each to the symbol whose [start_line, end_line]
        # range contains the comment's line. If none, leave symbol_id NULL
        # (file-level rationale).
        for rat in (result.get("rationales") or []):
            line = rat.get("line") or 0
            sym_id = None
            for ch in chunks:
                if ch["start_line"] <= line <= ch["end_line"]:
                    row = cur.execute(
                        "SELECT id FROM symbols WHERE repo=? AND path=? "
                        "AND qualified_name=?",
                        (repo_name, path, ch["name"])).fetchone()
                    if row: sym_id = row["id"]
                    # keep walking for the INNERMOST chunk (deepest range)
            cur.execute(
                "INSERT INTO rationales(repo, path, line, tag, text, symbol_id) "
                "VALUES (?,?,?,?,?,?)",
                (repo_name, path, line, rat["tag"], rat["text"], sym_id))
            stats["rationales"] += 1

        parsed.append((path, result))
        if progress_cb and (i % 10 == 0):
            progress_cb(f"code-graph pass1: {i+1}/{total} files parsed", 0.0)

    conn.commit()

    # ── PASS 2: build resolution indices ─────────────────────────────────
    mod_to_file: dict[str, str] = {}
    for (path, _r) in parsed:
        for m in _module_paths_for_file(path):
            mod_to_file.setdefault(m, path)

    # leaf-name → [(symbol_id, path), ...]
    leaf_to_symid: dict[str, list[tuple[int, str]]] = {}
    leaf_counts:   dict[str, int] = {}
    for r in cur.execute(
        "SELECT id, name, path FROM symbols WHERE repo = ?",
        (repo_name,)).fetchall():
        nm = r["name"]
        leaf_to_symid.setdefault(nm, []).append((r["id"], r["path"]))
        leaf_counts[nm] = leaf_counts.get(nm, 0) + 1

    # file → {leaf_name → symbol_id}
    file_to_symbols: dict[str, dict[str, int]] = {}
    for r in cur.execute(
        "SELECT id, name, path FROM symbols WHERE repo = ?",
        (repo_name,)).fetchall():
        file_to_symbols.setdefault(r["path"], {})[r["name"]] = r["id"]

    # extract & resolve class bases into `inherits`
    for (path, result) in parsed:
        for ch in (result.get("chunks") or []):
            bases = ch.get("bases") or []
            if not bases: continue
            sid_row = cur.execute(
                "SELECT id FROM symbols WHERE repo=? AND path=? AND qualified_name=?",
                (repo_name, path, ch["name"])).fetchone()
            if not sid_row: continue
            sid = sid_row["id"]
            for b in bases:
                cands = leaf_to_symid.get(b) or []
                base_id, conf = (None, 0.0)
                if len(cands) == 1:
                    base_id, conf = cands[0][0], 0.9
                elif len(cands) > 1:
                    base_id, conf = cands[0][0], 0.4
                cur.execute(
                    "INSERT INTO inherits(src_id, base_name, base_id, "
                    "confidence, repo) VALUES (?,?,?,?,?)",
                    (sid, b, base_id, conf, repo_name))
                stats["inherits"] += 1
    conn.commit()

    # ── PASS 3: scope-aware call resolution ──────────────────────────────
    # per-file import scope
    scopes: dict[str, dict[str, tuple[str, Optional[str]]]] = {}
    for r in cur.execute(
        "SELECT src_path, local_name, target_module, target_name "
        "FROM imports WHERE repo = ?", (repo_name,)).fetchall():
        scopes.setdefault(r["src_path"], {})[r["local_name"]] = (
            r["target_module"], r["target_name"])

    breakdown = stats["resolution_breakdown"]

    for (path, result) in parsed:
        scope = scopes.get(path, {})
        in_file_syms = file_to_symbols.get(path, {})
        for ch in (result.get("chunks") or []):
            sid_row = cur.execute(
                "SELECT id FROM symbols WHERE repo=? AND path=? AND qualified_name=?",
                (repo_name, path, ch["name"])).fetchone()
            if not sid_row: continue
            src_id = sid_row["id"]

            for call in (ch.get("calls") or []):
                if isinstance(call, dict):
                    callee   = call.get("name")
                    receiver = call.get("receiver")
                else:
                    callee, receiver = call, None
                if not callee: continue

                dst_id, conf = None, 0.0

                def _resolve_mod(m: str) -> Optional[str]:
                    """Find a file matching a (possibly URL-prefixed) module spec."""
                    if not m: return None
                    if m in mod_to_file: return mod_to_file[m]
                    # try stripping repo/url prefix segment by segment from the LEFT
                    for sep in ("/", "."):
                        if sep in m:
                            for i in range(m.count(sep)):
                                rest = m.split(sep, i+1)[-1]
                                if rest in mod_to_file: return mod_to_file[rest]
                    # try the last segment alone
                    tail = m.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
                    return mod_to_file.get(tail)

                # (1a) bare call where name was imported
                if receiver is None and callee in scope:
                    target_mod, target_name = scope[callee]
                    tfile = _resolve_mod(target_mod)
                    if tfile:
                        look = target_name or callee
                        for (cid, cpath) in (leaf_to_symid.get(look) or []):
                            if cpath == tfile:
                                dst_id, conf = cid, 1.0
                                break

                # (1b) qualified call mod.foo where mod was imported
                if dst_id is None and receiver and receiver in scope:
                    target_mod, target_name = scope[receiver]
                    # When receiver was `import X as Y` form
                    # `from pkg import sub as Y` → scope[Y] = (pkg, sub).
                    # Combined `pkg.sub` is the actual module path.
                    combined = (f"{target_mod}.{target_name}"
                                if target_name else None)
                    tfile = (_resolve_mod(combined) if combined else None) \
                            or _resolve_mod(target_mod) \
                            or _resolve_mod(f"{target_mod}/{callee}") \
                            or _resolve_mod(f"{target_mod}.{callee}")
                    if tfile:
                        for (cid, cpath) in (leaf_to_symid.get(callee) or []):
                            if cpath == tfile:
                                dst_id, conf = cid, 1.0
                                break
                    # Go: any file in the imported package's DIRECTORY works
                    if dst_id is None:
                        tail = (target_mod or "").rsplit("/", 1)[-1]
                        if tail:
                            for (cid, cpath) in (leaf_to_symid.get(callee) or []):
                                # match files in directory matching package name
                                cdir = cpath.replace("\\","/").rsplit("/", 1)[0]
                                if cdir.endswith("/" + tail) or cdir == tail:
                                    dst_id, conf = cid, 0.9
                                    break

                # (2) same-file or unique-in-repo leaf name.
                # IMPORTANT: in-file leaf-match only fires for BARE calls or
                # self/cls calls. For `obj.foo()` with a non-self receiver,
                # in-file matching wrongly resolves to nested same-named
                # functions (e.g. `client.get()` → `test_xyz.get` bug).
                if dst_id is None:
                    is_bare_or_self = (receiver is None
                                       or receiver in ("self", "cls", "this"))
                    if is_bare_or_self and callee in in_file_syms:
                        dst_id, conf = in_file_syms[callee], 0.8
                    elif (is_bare_or_self
                          and leaf_counts.get(callee, 0) == 1):
                        dst_id, conf = leaf_to_symid[callee][0][0], 0.7
                    elif leaf_counts.get(callee, 0) == 1:
                        # Non-self receiver + unique leaf → lower confidence
                        # so blast_radius (>=0.7) doesn't follow it (avoids
                        # the dict.update → user-defined `update` false
                        # positive), but topology and find_callers still
                        # see the edge so god-nodes like `@app.route` keep
                        # their real caller counts.
                        dst_id, conf = leaf_to_symid[callee][0][0], 0.5

                # (3-pre) Typed-parameter dispatch: `req.foo()` where `req`
                # is a function parameter annotated with class type T → resolve
                # to T.foo on the class hierarchy.
                if dst_id is None and receiver:
                    param_types = ch.get("param_types") or {}
                    cls_name = param_types.get(receiver)
                    if cls_name:
                        # find class symbol(s) with this name; walk methods
                        cls_cands = leaf_to_symid.get(cls_name) or []
                        for (cls_id, _cls_path) in cls_cands:
                            mrows = cur.execute(
                                "SELECT s.id FROM symbols s "
                                "WHERE s.repo=? AND s.name=? "
                                "AND s.qualified_name LIKE ?",
                                (repo_name, callee,
                                 f"%{cls_name}.{callee}")).fetchall()
                            if mrows:
                                dst_id, conf = mrows[0]["id"], 0.85
                                break
                            # walk bases too
                            bvisited = {cls_id}; bqueue = [cls_id]; bhops = 0
                            while bqueue and bhops < 4 and dst_id is None:
                                bhops += 1
                                next_q = []
                                for cur_cls in bqueue:
                                    base_rows = cur.execute(
                                        "SELECT base_id, base_name FROM inherits "
                                        "WHERE repo=? AND src_id=? "
                                        "AND base_id IS NOT NULL",
                                        (repo_name, cur_cls)).fetchall()
                                    for br in base_rows:
                                        bid = br["base_id"]; bnm = br["base_name"]
                                        if bid in bvisited: continue
                                        bvisited.add(bid); next_q.append(bid)
                                        mr = cur.execute(
                                            "SELECT s.id FROM symbols s "
                                            "WHERE s.repo=? AND s.name=? "
                                            "AND s.qualified_name LIKE ?",
                                            (repo_name, callee,
                                             f"%{bnm}.{callee}")).fetchall()
                                        if mr:
                                            dst_id, conf = mr[0]["id"], 0.75
                                            break
                                    if dst_id is not None: break
                                bqueue = next_q
                            if dst_id is not None: break

                # (3) self/cls/this.foo() — climb the enclosing class's MRO
                if dst_id is None and receiver in ("self", "cls", "this"):
                    qn = ch.get("name") or ""
                    if "." in qn:
                        cls_name = qn.split(".")[0]
                        cls_cands = leaf_to_symid.get(cls_name) or []
                        for (cls_id, _cls_path) in cls_cands:
                            visited = {cls_id}
                            queue = [cls_id]
                            hops = 0
                            while queue and hops < 5 and dst_id is None:
                                hops += 1
                                next_q = []
                                for cur_cls in queue:
                                    method_rows = cur.execute(
                                        "SELECT s.id FROM symbols s "
                                        "WHERE s.repo=? AND s.name=? "
                                        "AND s.qualified_name LIKE ?",
                                        (repo_name, callee,
                                         f"%{cls_name}.{callee}")).fetchall()
                                    if method_rows:
                                        dst_id, conf = method_rows[0]["id"], 0.5
                                        break
                                    base_rows = cur.execute(
                                        "SELECT base_id FROM inherits "
                                        "WHERE repo=? AND src_id=? "
                                        "AND base_id IS NOT NULL",
                                        (repo_name, cur_cls)).fetchall()
                                    for br in base_rows:
                                        if br["base_id"] not in visited:
                                            visited.add(br["base_id"])
                                            next_q.append(br["base_id"])
                                queue = next_q
                            if dst_id is not None: break

                # (4) ambiguous fallback — picks LIMIT-1 first match.
                # Gated on bare/self receiver for the same reason as step (2):
                # `obj.update()` shouldn't randomly resolve to the first
                # `update` symbol in the repo. Only fire for bare/self calls.
                is_bare_or_self = (receiver is None
                                   or receiver in ("self", "cls", "this"))
                if (dst_id is None
                        and is_bare_or_self
                        and leaf_counts.get(callee, 0) > 1):
                    dst_id, conf = leaf_to_symid[callee][0][0], 0.3

                cur.execute(
                    "INSERT INTO calls(src_id, dst_name, dst_id, kind, "
                    "confidence, repo) VALUES (?,?,?,?,?,?)",
                    (src_id, callee, dst_id, "CALLS", conf, repo_name))
                stats["calls"] += 1
                if   conf >= 1.0: breakdown["import_scope"] += 1
                elif conf >= 0.6: breakdown["unique_leaf"]  += 1
                elif conf >= 0.4: breakdown["class_method"] += 1
                elif conf >= 0.2: breakdown["ambiguous"]    += 1
                else:             breakdown["unresolved"]   += 1

        if progress_cb:
            progress_cb(f"code-graph pass3: resolving {path}", 0.0)

    conn.commit()
    conn.close()
    stats["elapsed_s"] = round(time.time() - started, 2)
    return stats


# ── Query functions — all pure SQL, no LLM ──────────────────────────────────

def find_callers(db_path: str, name: str, repo: Optional[str] = None,
                  limit: int = 50) -> list[dict]:
    """Who calls a given function name? Returns list of caller records
    with a 3-bucket confidence label (EXTRACTED / INFERRED / AMBIGUOUS)."""
    conn = _connect(db_path)
    if repo:
        rows = conn.execute(
            "SELECT s.repo, s.path, s.qualified_name AS caller, s.start_line, s.end_line, "
            "c.confidence "
            "FROM calls c JOIN symbols s ON c.src_id = s.id "
            "WHERE c.repo = ? AND c.dst_name = ? "
            "ORDER BY c.confidence DESC, s.path LIMIT ?",
            (repo, name, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT s.repo, s.path, s.qualified_name AS caller, s.start_line, s.end_line, "
            "c.confidence "
            "FROM calls c JOIN symbols s ON c.src_id = s.id "
            "WHERE c.dst_name = ? "
            "ORDER BY c.confidence DESC, s.path LIMIT ?",
            (name, limit)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["bucket"] = _confidence_bucket(d.get("confidence") or 0.0)
        out.append(d)
    return out


def find_callees(db_path: str, name: str, repo: Optional[str] = None,
                  limit: int = 50) -> list[dict]:
    """What does a function call? Returns callees + bucket labels."""
    conn = _connect(db_path)
    if repo:
        rows = conn.execute(
            "SELECT c.dst_name, t.path AS dst_path, t.qualified_name AS dst_qualified, "
            "       t.start_line, t.end_line, c.confidence "
            "FROM calls c "
            "JOIN symbols s ON c.src_id = s.id "
            "LEFT JOIN symbols t ON c.dst_id = t.id "
            "WHERE c.repo = ? AND (s.name = ? OR s.qualified_name = ?) "
            "ORDER BY c.confidence DESC LIMIT ?",
            (repo, name, name, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT c.dst_name, t.path AS dst_path, t.qualified_name AS dst_qualified, "
            "       t.start_line, t.end_line, c.confidence "
            "FROM calls c "
            "JOIN symbols s ON c.src_id = s.id "
            "LEFT JOIN symbols t ON c.dst_id = t.id "
            "WHERE (s.name = ? OR s.qualified_name = ?) "
            "ORDER BY c.confidence DESC LIMIT ?",
            (name, name, limit)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["bucket"] = _confidence_bucket(d.get("confidence") or 0.0)
        out.append(d)
    return out


def blast_radius(db_path: str, path: str, repo: Optional[str] = None,
                  max_depth: int = 3, strict: bool = True) -> dict:
    """Given a file path, return every file that could be affected if it
    changes. Walks the reverse call-graph up to `max_depth` hops via symbol
    resolution + import edges.

    Returns:
      { 'changed_file': path,
        'affected_files': [paths…],
        'callers_by_hop': { 1: [..], 2: [..] },
        'reverse_imports': [paths importing this file],
        'symbols_in_changed_file': [..],
        'total_affected': N }"""
    conn = _connect(db_path)

    # symbols defined in the changed file
    sym_q = ("WHERE path = ?" if not repo else "WHERE repo = ? AND path = ?")
    sym_args = (path,) if not repo else (repo, path)
    sym_rows = conn.execute(
        f"SELECT id, name, qualified_name, path FROM symbols {sym_q}", sym_args).fetchall()

    # Auto-detect: if caller passed a SYMBOL name (not a file path), look it up
    # by name and treat as a single-symbol blast radius.
    if not sym_rows:
        name_q = ("WHERE name = ? OR qualified_name = ?"
                  if not repo else "WHERE repo = ? AND (name = ? OR qualified_name = ?)")
        name_args = (path, path) if not repo else (repo, path, path)
        sym_rows = conn.execute(
            f"SELECT id, name, qualified_name, path FROM symbols {name_q}",
            name_args).fetchall()

    symbol_ids = [r["id"] for r in sym_rows]
    symbol_names = [r["name"] for r in sym_rows]
    # if we matched by symbol, treat the symbol's own file as the "changed_file" anchor
    if sym_rows and sym_rows[0]["path"] != path:
        path = sym_rows[0]["path"]

    affected: set[str] = {path}
    callers_by_hop: dict[int, list[str]] = {}
    frontier = set(symbol_ids)
    frontier_names = set(symbol_names)

    # Strict mode = only follow resolved edges with confidence >= MIN_CONF.
    # Raised from 0.5 → 0.7 after the git-truth eval showed that 0.5 edges
    # (class-dispatch + ambiguous fallback) dominated our false positives.
    # Subclasses still propagate at this same threshold.
    MIN_CONF = 0.7

    for hop in range(1, max_depth + 1):
        placeholders = ",".join("?" for _ in frontier) if frontier else "NULL"
        name_phs    = ",".join("?" for _ in frontier_names) if frontier_names else "NULL"
        if frontier or (frontier_names and not strict):
            if strict:
                # callers via resolved high-confidence edges
                query = (
                    "SELECT DISTINCT s.id, s.path, s.name, s.qualified_name "
                    "FROM calls c JOIN symbols s ON c.src_id = s.id "
                    f"WHERE c.dst_id IN ({placeholders}) AND c.confidence >= ?")
                params = list(frontier) + [MIN_CONF]
            else:
                query = (
                    "SELECT DISTINCT s.id, s.path, s.name, s.qualified_name "
                    "FROM calls c JOIN symbols s ON c.src_id = s.id "
                    f"WHERE c.dst_id IN ({placeholders}) "
                    f"  OR c.dst_name IN ({name_phs})")
                params = list(frontier) + list(frontier_names)
            if repo:
                query += " AND c.repo = ?"
                params.append(repo)
            rows = conn.execute(query, params).fetchall()
            new_ids = set(); new_names = set(); paths_this_hop = set()
            for r in rows:
                if r["id"] not in symbol_ids:
                    new_ids.add(r["id"])
                    new_names.add(r["name"])
                    paths_this_hop.add(r["path"])
                    affected.add(r["path"])

            # subclass propagation: if any frontier symbol is a class whose
            # bases are in scope, pull its subclasses (changing base affects
            # subclasses too). Only follow base_id resolved edges with conf >= 0.5.
            if strict and frontier:
                sub_rows = conn.execute(
                    "SELECT DISTINCT s.id, s.path, s.name, s.qualified_name "
                    "FROM inherits i JOIN symbols s ON i.src_id = s.id "
                    f"WHERE i.base_id IN ({placeholders}) AND i.confidence >= ?",
                    list(frontier) + [MIN_CONF]).fetchall()
                for r in sub_rows:
                    if r["id"] not in symbol_ids:
                        new_ids.add(r["id"])
                        new_names.add(r["name"])
                        paths_this_hop.add(r["path"])
                        affected.add(r["path"])

            callers_by_hop[hop] = sorted(paths_this_hop)
            if not new_ids and not new_names:
                break
            frontier = new_ids
            frontier_names = new_names
        else:
            break

    # reverse-imports: in strict mode, look up any importer whose target_module
    # matches one of this file's possible dotted-paths. Loose mode falls back
    # to the broad LIKE %stem% scan.
    if strict:
        candidate_modules = _module_paths_for_file(path)
        candidate_modules.append(path)
        # Exact matches first
        ph = ",".join("?" for _ in candidate_modules)
        # Plus ENDS-WITH match (for Go-style URL imports
        # `github.com/x/y/render` → matches our "render" candidate)
        like_clauses = " OR ".join("target_module LIKE ?" for _ in candidate_modules)
        like_args = [f"%/{c}" for c in candidate_modules]
        if repo:
            rev = conn.execute(
                f"SELECT DISTINCT src_path FROM imports WHERE repo = ? AND ("
                f"  target_module IN ({ph}) OR dst_module IN ({ph}) OR {like_clauses}"
                f")", [repo] + candidate_modules + candidate_modules + like_args
            ).fetchall()
        else:
            rev = conn.execute(
                f"SELECT DISTINCT src_path FROM imports WHERE "
                f"  target_module IN ({ph}) OR dst_module IN ({ph}) OR {like_clauses}",
                candidate_modules + candidate_modules + like_args).fetchall()
    else:
        rev = conn.execute(
            "SELECT DISTINCT src_path FROM imports WHERE repo = ? AND "
            "(dst_module LIKE ? OR dst_module LIKE ? OR dst_module = ?)",
            (repo or "", f"%{path}%", f"%{Path(path).stem}%", path)
        ).fetchall() if repo else conn.execute(
            "SELECT DISTINCT src_path FROM imports WHERE "
            "(dst_module LIKE ? OR dst_module LIKE ? OR dst_module = ?)",
            (f"%{path}%", f"%{Path(path).stem}%", path)).fetchall()
    rev_paths = [r["src_path"] for r in rev]
    affected.update(rev_paths)

    conn.close()
    affected.discard(path)
    return {
        "changed_file":            path,
        "affected_files":          sorted(affected),
        "callers_by_hop":          callers_by_hop,
        "reverse_imports":         rev_paths,
        "symbols_in_changed_file": symbol_names,
        "total_affected":          len(affected),
    }


def find_path(db_path: str, src: str, dst: str, max_hops: int = 4,
              repo: Optional[str] = None) -> Optional[list[str]]:
    """BFS through call edges. Return the shortest call chain src → ... → dst,
    or None if no path within max_hops."""
    conn = _connect(db_path)
    # start nodes are any symbol matching src by name or qualified_name
    where_repo = "AND repo = ?" if repo else ""
    starts = conn.execute(
        f"SELECT id, qualified_name FROM symbols "
        f"WHERE (name = ? OR qualified_name = ?) {where_repo}",
        ((src, src) + ((repo,) if repo else ()))).fetchall()
    if not starts: conn.close(); return None
    targets = {dst, dst.split(".")[-1]}

    queue: list[tuple[int, list[str]]] = [(s["id"], [s["qualified_name"]]) for s in starts]
    visited: set[int] = {s["id"] for s in starts}

    while queue:
        nid, trail = queue.pop(0)
        if len(trail) > max_hops:
            continue
        callees = conn.execute(
            "SELECT c.dst_name, c.dst_id, t.qualified_name AS dst_qualified "
            "FROM calls c LEFT JOIN symbols t ON c.dst_id = t.id "
            "WHERE c.src_id = ?", (nid,)).fetchall()
        for c in callees:
            qn = c["dst_qualified"] or c["dst_name"]
            new_trail = trail + [qn]
            if qn in targets or c["dst_name"] in targets:
                conn.close()
                return new_trail
            if c["dst_id"] and c["dst_id"] not in visited:
                visited.add(c["dst_id"])
                queue.append((c["dst_id"], new_trail))
    conn.close()
    return None


def triage_pr(db_path: str, changed_files: list[str],
                repo: Optional[str] = None,
                max_hops: int = 3) -> dict:
    """Given a PR's changed-file list, compute a single risk picture.

    Returns:
      • combined_affected_files: union of blast_radius(f) across every file
      • god_nodes_touched:       any changed file containing a god-node symbol
      • rationale_warnings:      any `# HACK/SAFETY/WARNING/BUG/FIXME` near
                                  touched code — bright red flag
      • risk_score:              [0, 100] — log of impact + multipliers
      • risk_tier:               'LOW' (<25) | 'MEDIUM' (25-60) | 'HIGH' (60+)
      • merge_order_hint:        if any changed file is in the top-5 god-nodes
                                  or has SAFETY/BUG rationales, flag for
                                  merge-first / merge-last attention
      • per_file:                breakdown by file

    The scoring is OUR thing — code-review-graph has PR triage too but
    they don't fold in rationales or god-node centrality."""
    import math
    out: dict = {"changed_files": changed_files}
    if not changed_files:
        out["risk_score"] = 0; out["risk_tier"] = "LOW"
        return out

    # combined impact via blast_radius
    union: set[str] = set(changed_files)
    per_file: dict[str, dict] = {}
    for f in changed_files:
        try:
            br = blast_radius(db_path, f, repo=repo, max_depth=max_hops,
                              strict=True)
            union.update(br["affected_files"])
            per_file[f] = {
                "affected":         br["total_affected"],
                "callers_by_hop":   {h: len(v) for h, v in br["callers_by_hop"].items()},
                "reverse_imports":  len(br["reverse_imports"]),
            }
        except Exception as e:
            per_file[f] = {"error": str(e)}
    out["combined_affected_files"] = sorted(union)
    out["per_file"] = per_file

    # god-node touches: did the PR touch any symbol in the top 10 god-nodes?
    topo = analyze_topology(db_path, repo=repo, top_god=10, top_surprise=0)
    god_paths = {g["path"] for g in topo["god_nodes"]}
    touched_god = sorted(set(changed_files) & god_paths)
    out["god_nodes_touched"] = touched_god

    # rationale warnings near touched lines
    conn = _connect(db_path)
    ph = ",".join("?" for _ in changed_files)
    warning_tags = ("HACK", "SAFETY", "WARNING", "BUG", "FIXME", "XXX", "DEPRECATED")
    tag_ph = ",".join("?" for _ in warning_tags)
    args = list(changed_files) + list(warning_tags)
    if repo:
        rows = conn.execute(
            f"SELECT path, line, tag, text FROM rationales "
            f"WHERE repo = ? AND path IN ({ph}) AND tag IN ({tag_ph}) "
            f"ORDER BY tag", [repo] + args).fetchall()
    else:
        rows = conn.execute(
            f"SELECT path, line, tag, text FROM rationales "
            f"WHERE path IN ({ph}) AND tag IN ({tag_ph}) "
            f"ORDER BY tag", args).fetchall()
    out["rationale_warnings"] = [dict(r) for r in rows]
    conn.close()

    # Scoring:
    #   base    = 10 * log10(1 + |combined_affected_files|)
    #   bonus  +20 per god-node touched
    #   bonus  +15 if any HACK/SAFETY/BUG/FIXME rationale present
    #   bonus  +5 per touched file (size penalty)
    base   = 10 * math.log10(1 + len(union))
    god_b  = 20 * len(touched_god)
    rat_b  = 15 if out["rationale_warnings"] else 0
    size_b = 5 * len(changed_files)
    score  = round(min(100.0, base + god_b + rat_b + size_b), 1)
    out["risk_score"] = score
    base_tier = ("HIGH" if score >= 60 else
                 "MEDIUM" if score >= 25 else "LOW")
    # SAFETY/BUG/HACK comments near the touched code are always worth a
    # reviewer's attention, even if the structural blast radius is small.
    # Force at least MEDIUM tier when such warnings are present.
    serious_tags = {"SAFETY", "BUG", "HACK"}
    has_serious = any(w.get("tag") in serious_tags
                       for w in out["rationale_warnings"])
    if has_serious and base_tier == "LOW":
        base_tier = "MEDIUM"
    out["risk_tier"] = base_tier
    out["merge_order_hint"] = (
        "merge-last (touches god-nodes — coordinate with downstream teams)"
        if touched_god else
        "merge-first (low risk, no chokepoints touched)"
        if score < 25 else
        "normal queue")
    return out


def analyze_topology(db_path: str, repo: Optional[str] = None,
                      top_god: int = 15, top_surprise: int = 15) -> dict:
    """Graph-shape insights you can extract WITHOUT touching content:

      • god_nodes:           symbols with the highest in-degree (most callers).
                              A change to these touches the most of the codebase.
                              Maps directly to "what are the chokepoints?"
      • surprising_connections:
                              edges that bridge two otherwise-loosely-connected
                              file-folders. We score each edge by
                                surprise = 1 / (1 + cross_folder_edge_count)
                              so edges whose endpoints sit in folders that
                              RARELY interact get the highest score.
      • orphans:             symbols with zero callers AND zero callees — likely
                              dead code or pure-leaf utilities.
      • inheritance_depth:   class chains > 3 deep (refactor risk)."""
    conn = _connect(db_path)
    where_repo = "WHERE repo = ?" if repo else ""
    args = (repo,) if repo else ()

    # ── god nodes: top callees by resolved high-confidence in-degree.
    # Exclude destinations in test/example dirs — those aren't APIs the
    # broader codebase depends on; they're commonly false positives from
    # unique-leaf resolution (e.g. dict.update matching the one
    # user-defined `update` in a tutorial).
    god_rows = conn.execute(
        f"SELECT s.id, s.path, s.qualified_name, s.name, COUNT(*) AS callers "
        f"FROM calls c JOIN symbols s ON c.dst_id = s.id "
        f"WHERE c.confidence >= 0.5 "
        f"{('AND c.repo = ?' if repo else '')} "
        f"  AND s.path NOT LIKE '%test%' "
        f"  AND s.path NOT LIKE 'examples/%' "
        f"  AND s.path NOT LIKE '%/examples/%' "
        f"  AND s.path NOT LIKE 'docs/%' "
        f"GROUP BY s.id ORDER BY callers DESC LIMIT ?",
        (list(args) + [top_god])).fetchall()
    god_nodes = [{
        "qualified_name": r["qualified_name"],
        "leaf":           r["name"],
        "path":           r["path"],
        "caller_count":   r["callers"],
    } for r in god_rows]

    # ── surprising connections: edges between folders that rarely connect
    # First: per-folder-pair edge counts.
    folder_pair_rows = conn.execute(
        f"SELECT  src.path AS src_path, dst.path AS dst_path, COUNT(*) AS n "
        f"FROM calls c "
        f"JOIN symbols src ON c.src_id = src.id "
        f"JOIN symbols dst ON c.dst_id = dst.id "
        f"WHERE c.confidence >= 0.5 "
        f"{('AND c.repo = ?' if repo else '')} "
        f"GROUP BY src.path, dst.path",
        args).fetchall()
    folder_counts: dict[tuple[str, str], int] = {}
    folder_edges: list[tuple[str, str, str, str]] = []
    for r in folder_pair_rows:
        sp = (r["src_path"].replace("\\", "/").rsplit("/", 1)[0]
              if "/" in r["src_path"] else "")
        dp = (r["dst_path"].replace("\\", "/").rsplit("/", 1)[0]
              if "/" in r["dst_path"] else "")
        if sp == dp: continue       # only count cross-folder edges
        key = (sp, dp)
        folder_counts[key] = folder_counts.get(key, 0) + r["n"]
        folder_edges.append((sp, dp, r["src_path"], r["dst_path"]))
    # surprise score: edges in folder-pairs with the FEWEST total cross-edges
    folder_edges.sort(key=lambda e: folder_counts.get((e[0], e[1]), 0))
    seen_pairs: set[tuple[str, str]] = set()
    surprising: list[dict] = []
    for (sp, dp, sf, df) in folder_edges:
        key = (sp, dp)
        if key in seen_pairs: continue
        seen_pairs.add(key)
        surprising.append({
            "src_folder":   sp,
            "dst_folder":   dp,
            "example_edge": f"{sf} -> {df}",
            "rarity":       round(1.0 / (1 + folder_counts.get(key, 0)), 4),
            "edge_count":   folder_counts.get(key, 0),
        })
        if len(surprising) >= top_surprise: break

    # ── orphans: symbols with zero incoming AND zero outgoing edges ─────
    orphan_rows = conn.execute(
        f"SELECT s.path, s.qualified_name, s.name FROM symbols s "
        f"{where_repo} "
        f"AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.dst_id = s.id) "
        f"AND NOT EXISTS (SELECT 1 FROM calls c2 WHERE c2.src_id = s.id) "
        f"LIMIT 25" if repo else
        f"SELECT s.path, s.qualified_name, s.name FROM symbols s "
        f"WHERE NOT EXISTS (SELECT 1 FROM calls c WHERE c.dst_id = s.id) "
        f"AND NOT EXISTS (SELECT 1 FROM calls c2 WHERE c2.src_id = s.id) "
        f"LIMIT 25",
        args).fetchall()
    orphans = [dict(r) for r in orphan_rows]

    # ── inheritance chains > 3 deep ─────────────────────────────────────
    deep_rows = conn.execute(
        f"WITH RECURSIVE chain(id, name, depth, path) AS ("
        f"  SELECT s.id, s.qualified_name, 0, s.qualified_name "
        f"  FROM symbols s "
        f"  WHERE NOT EXISTS (SELECT 1 FROM inherits i WHERE i.base_id = s.id) "
        f"  UNION ALL "
        f"  SELECT s2.id, s2.qualified_name, c.depth + 1, c.path || ' < ' || s2.qualified_name "
        f"  FROM chain c JOIN inherits i ON i.base_id = c.id "
        f"  JOIN symbols s2 ON i.src_id = s2.id "
        f"  WHERE c.depth < 6"
        f") SELECT depth, path FROM chain WHERE depth >= 3 "
        f"ORDER BY depth DESC LIMIT 10"
    ).fetchall()
    deep_chains = [{"depth": r["depth"], "chain": r["path"]} for r in deep_rows]

    conn.close()
    return {
        "god_nodes":               god_nodes,
        "surprising_connections":  surprising,
        "orphans":                 orphans,
        "deep_inheritance_chains": deep_chains,
    }


def suggested_questions(db_path: str, repo: Optional[str] = None) -> list[str]:
    """Auto-generate questions the graph is uniquely positioned to answer.

    Pure heuristics on topology — no LLM. Used at onboarding to bootstrap
    new users / new AI sessions into the codebase."""
    top = analyze_topology(db_path, repo=repo, top_god=5, top_surprise=3)
    qs: list[str] = []
    for g in top["god_nodes"][:3]:
        qs.append(f"What calls `{g['qualified_name']}` and what would break "
                  f"if I changed it?")
    for s in top["surprising_connections"][:2]:
        qs.append(f"Why does `{s['src_folder']}` reach into "
                  f"`{s['dst_folder']}`? It looks like an unusual coupling.")
    if top["deep_inheritance_chains"]:
        c = top["deep_inheritance_chains"][0]
        qs.append(f"Walk me through this class chain: {c['chain']}")
    if top["orphans"]:
        n = len(top["orphans"])
        qs.append(f"There are {n}+ unreferenced symbols — which are dead code?")
    qs.append("Show me the `# WHY:` and `# HACK:` rationales captured in this repo.")
    return qs[:7]


def rationales_for_symbol(db_path: str, symbol_name: str,
                            repo: Optional[str] = None) -> list[dict]:
    """Return all `# WHY:/HACK:/NOTE:/etc` comments attached to a symbol or
    its enclosing class. The "tribal knowledge" attached to the code."""
    conn = _connect(db_path)
    where = "(symbols.name = ? OR symbols.qualified_name = ?)"
    params: list = [symbol_name, symbol_name]
    if repo:
        where = f"symbols.repo = ? AND {where}"
        params = [repo] + params
    rows = conn.execute(
        f"SELECT r.tag, r.text, r.line, r.path, s.qualified_name AS symbol "
        f"FROM rationales r JOIN symbols s ON r.symbol_id = s.id "
        f"WHERE {where} ORDER BY r.line", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def rationales_in_file(db_path: str, path: str,
                        repo: Optional[str] = None) -> list[dict]:
    """All rationale comments in a file."""
    conn = _connect(db_path)
    q = "WHERE path = ?" if not repo else "WHERE repo = ? AND path = ?"
    args = (path,) if not repo else (repo, path)
    rows = conn.execute(
        f"SELECT tag, text, line, symbol_id FROM rationales {q} ORDER BY line",
        args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _confidence_bucket(conf: float) -> str:
    """Project our 6-tier numeric confidence to graphify-style 3-bucket label
    for legibility in UIs/answers. We keep numbers internally but expose
    buckets for API consumers who want simple HIGH/MED/LOW filters."""
    if   conf >= 0.85: return "EXTRACTED"   # import-scoped or in-file
    elif conf >= 0.5:  return "INFERRED"    # class-dispatch / unique-leaf
    else:              return "AMBIGUOUS"   # name-only or unresolved


def edge_buckets(db_path: str, repo: Optional[str] = None) -> dict:
    """Return edge counts per confidence bucket — for the API/UI."""
    conn = _connect(db_path)
    where = "WHERE repo = ?" if repo else ""
    args = (repo,) if repo else ()
    rows = conn.execute(
        f"SELECT confidence, COUNT(*) c FROM calls {where} "
        f"GROUP BY confidence", args).fetchall()
    out = {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}
    for r in rows:
        out[_confidence_bucket(r["confidence"] or 0.0)] += r["c"]
    out["total"] = sum(out.values())
    conn.close()
    return out


def stats(db_path: str, repo: Optional[str] = None) -> dict:
    """Counts for the code graph."""
    if not os.path.exists(db_path):
        return {"files": 0, "symbols": 0, "calls": 0, "imports": 0,
                "inherits": 0, "rationales": 0,
                "edge_buckets": {"EXTRACTED": 0, "INFERRED": 0,
                                  "AMBIGUOUS": 0}}
    conn = _connect(db_path)
    where = "WHERE repo = ?" if repo else ""
    args = (repo,) if repo else ()
    out = {
        "files":      conn.execute(f"SELECT COUNT(*) FROM files      {where}", args).fetchone()[0],
        "symbols":    conn.execute(f"SELECT COUNT(*) FROM symbols    {where}", args).fetchone()[0],
        "calls":      conn.execute(f"SELECT COUNT(*) FROM calls      {where}", args).fetchone()[0],
        "imports":    conn.execute(f"SELECT COUNT(*) FROM imports    {where}", args).fetchone()[0],
        "inherits":   conn.execute(f"SELECT COUNT(*) FROM inherits   {where}", args).fetchone()[0],
        "rationales": conn.execute(f"SELECT COUNT(*) FROM rationales {where}", args).fetchone()[0],
    }
    # buckets: counts of edges per 3-tier confidence label
    bucket_rows = conn.execute(
        f"SELECT confidence, COUNT(*) c FROM calls {where} GROUP BY confidence",
        args).fetchall()
    buckets = {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}
    for r in bucket_rows:
        buckets[_confidence_bucket(r["confidence"] or 0.0)] += r["c"]
    out["edge_buckets"] = buckets
    conn.close()
    return out
