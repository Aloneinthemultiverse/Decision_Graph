"""Codebase ingestion — turn a local git repo into institutional memory.

Walks a repo and extracts the artifacts that capture WHY things are the way
they are (not just what the code does):

  · README*, CONTRIBUTING, CHANGELOG               → top-level prose
  · docs/, doc/, .github/                          → all .md / .rst / .txt
  · docs/adr/, doc/decisions/, architecture/       → ADRs (decision records)
  · CODEOWNERS                                     → who owns what
  · package metadata (package.json, pyproject.toml,
    Cargo.toml, go.mod)                            → stack / dependencies
  · git log --max-count=200                        → recent commit messages
                                                     as a decision timeline
  · top-level docstrings from .py files            → quick semantic index

Everything is stored as plain decisions tagged `[repo:<name>] <topic>` so it
shows up alongside the rest of DG memory and can be retrieved by `ask` /
`recall` / `query`.

Pure stdlib; runs in the main DG process. No external deps.
"""
from __future__ import annotations

import os
import re
import subprocess
import json
from pathlib import Path
from typing import Optional


# Patterns + limits
_DOC_GLOBS = [
    "README*", "CONTRIBUTING*", "CHANGELOG*", "ARCHITECTURE*",
    "docs/**/*.md", "docs/**/*.rst", "docs/**/*.txt",
    "doc/**/*.md", "doc/**/*.rst",
    ".github/**/*.md",
    # Multimodal-lite: architecture docs as PDFs + text-based diagrams
    "docs/**/*.pdf", "doc/**/*.pdf", "architecture/**/*.pdf",
    "docs/**/*.puml", "docs/**/*.mmd", "docs/**/*.drawio", "docs/**/*.dot",
    "architecture/**/*.puml", "architecture/**/*.mmd",
    "*.puml", "*.mmd",   # repo-root diagrams
]
_ADR_DIRS = ["docs/adr", "doc/decisions", "docs/decisions", "architecture/decisions"]
_PKG_FILES = ["package.json", "pyproject.toml", "Cargo.toml", "go.mod",
              "setup.py", "requirements.txt", "composer.json"]
_MAX_FILE_BYTES = 200_000          # don't fold giant generated files
_MAX_FILES = 200                   # cap per repo
_MAX_COMMITS = 200


def _safe_read(p: Path, max_bytes: int = _MAX_FILE_BYTES) -> Optional[str]:
    """Read a text file, auto-detecting BOM-prefixed UTF-16 / UTF-8.

    GitHub repos often contain README/docs saved as UTF-16 LE/BE with a BOM
    (e.g. anything edited with Windows Notepad). Reading those as UTF-8 with
    replace gives garbage like 'a\x00b\x00c\x00' that renders as 'a b c'.
    We sniff the first few bytes for a BOM and decode appropriately."""
    try:
        if p.stat().st_size > max_bytes:
            return None
        raw = p.read_bytes()
        if not raw:
            return ""
        # detect BOMs
        if raw.startswith(b"\xff\xfe"):
            return raw[2:].decode("utf-16-le", errors="replace")
        if raw.startswith(b"\xfe\xff"):
            return raw[2:].decode("utf-16-be", errors="replace")
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw[3:].decode("utf-8", errors="replace")
        # heuristic: lots of NUL bytes interleaved → UTF-16 without BOM
        if len(raw) >= 8 and raw[1::2].count(0) > len(raw) * 0.4:
            try:
                return raw.decode("utf-16-le", errors="replace")
            except Exception:
                pass
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


def _repo_name(repo_root: Path) -> str:
    # prefer the git remote's slug; fall back to dir name
    try:
        r = subprocess.run(["git", "-C", str(repo_root), "remote", "get-url",
                            "origin"], capture_output=True, text=True,
                           timeout=5)
        if r.returncode == 0:
            url = r.stdout.strip()
            m = re.search(r"[/:]([^/:]+/[^/]+?)(?:\.git)?$", url)
            if m:
                return m.group(1)
    except Exception:
        pass
    return repo_root.name


def _python_module_docstring(text: str) -> Optional[str]:
    """Extract the leading docstring (module-level) from a .py file."""
    text = text.lstrip()
    if not (text.startswith('"""') or text.startswith("'''")):
        return None
    quote = text[:3]
    end = text.find(quote, 3)
    if end == -1:
        return None
    doc = text[3:end].strip()
    return doc if doc else None


def _collect_documents(repo_root: Path) -> list[dict]:
    out: list[dict] = []
    seen: set[Path] = set()

    # 1. ADRs FIRST (Architecture Decision Records — these are pure gold,
    #    classify them as 'adr' before the generic docs glob can claim them)
    for d in _ADR_DIRS:
        adr_dir = repo_root / d
        if not adr_dir.is_dir():
            continue
        for p in sorted(adr_dir.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() not in (".md", ".rst", ".txt"):
                continue
            if p in seen:
                continue
            seen.add(p)
            txt = _safe_read(p)
            if txt:
                out.append({"path": str(p.relative_to(repo_root)),
                             "kind": "adr",
                             "text": txt})

    # 2. doc-ish files (excluding anything we already grabbed as ADR).
    # Multimodal-lite: PDFs and architecture diagrams go through a separate
    # text extractor — they're tagged 'arch_doc' so the blueprint can flag
    # them as primary architecture sources.
    from .codebase_ast import extract_text_from_any, _MULTIMODAL_EXTS
    for pat in _DOC_GLOBS:
        for p in repo_root.glob(pat):
            if not p.is_file() or p in seen:
                continue
            seen.add(p)
            if p.suffix.lower() in _MULTIMODAL_EXTS:
                meta = extract_text_from_any(p) or {}
                txt = (meta.get("text") or "").strip()
                if txt:
                    out.append({"path": str(p.relative_to(repo_root)),
                                 "kind": "arch_doc",
                                 "media_type": meta.get("kind"),
                                 "text": txt})
                continue
            txt = _safe_read(p)
            if txt:
                out.append({"path": str(p.relative_to(repo_root)),
                             "kind": "doc",
                             "text": txt})
            if len(out) >= _MAX_FILES:
                return out

    # 3. package manifests
    for fn in _PKG_FILES:
        p = repo_root / fn
        if p.is_file() and p not in seen:
            seen.add(p)
            txt = _safe_read(p)
            if txt:
                out.append({"path": fn, "kind": "manifest", "text": txt})

    # 4. CODEOWNERS
    for cand in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
        p = repo_root / cand
        if p.is_file() and p not in seen:
            seen.add(p)
            txt = _safe_read(p)
            if txt:
                out.append({"path": cand, "kind": "codeowners", "text": txt})

    # 5. python module docstrings (top-level only — fast, semantic)
    for p in sorted(repo_root.rglob("*.py")):
        if any(part.startswith(".") for part in p.relative_to(repo_root).parts):
            continue
        if "__pycache__" in p.parts or p in seen:
            continue
        txt = _safe_read(p, max_bytes=20_000)
        if not txt:
            continue
        doc = _python_module_docstring(txt)
        if doc and len(doc) > 40:
            out.append({"path": str(p.relative_to(repo_root)),
                         "kind": "module_docstring",
                         "text": doc[:2000]})
            if len(out) >= _MAX_FILES:
                return out

    return out[:_MAX_FILES]


def _collect_recent_commits(repo_root: Path, n: int = _MAX_COMMITS) -> list[dict]:
    """Last N commits as structured data (timeline of micro-decisions)."""
    fmt = "--format=%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "log", f"--max-count={n}", fmt],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        if r.returncode != 0 or not r.stdout:
            return []
    except Exception:
        return []
    raw = (r.stdout or "").split("\x1e")
    out: list[dict] = []
    for chunk in raw:
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        if len(parts) < 4:
            continue
        sha, author, iso_ts, subject = parts[:4]
        body = parts[4].strip() if len(parts) >= 5 else ""
        out.append({"sha": sha[:12], "author": author, "ts": iso_ts,
                     "subject": subject.strip(), "body": body[:1000]})
    return out


def ingest_repo(ws_dg, repo_root_path: str,
                include_commits: bool = True,
                include_files: bool = True) -> dict:
    """Walk the repo, store each artifact as a decision in ws_dg.memory.

    Returns a summary dict: counts per category, repo name, total stored.
    """
    repo_root = Path(repo_root_path).expanduser().resolve()
    if not repo_root.is_dir():
        return {"error": f"not a directory: {repo_root}"}
    if not (repo_root / ".git").exists():
        return {"error": f"not a git repository (no .git): {repo_root}"}

    name = _repo_name(repo_root)
    stats = {"repo": name, "path": str(repo_root),
             "doc": 0, "adr": 0, "manifest": 0, "codeowners": 0,
             "module_docstring": 0, "commit": 0, "stored": 0, "skipped": 0}
    learnings_written: list[str] = []

    if include_files:
        for art in _collect_documents(repo_root):
            kind = art["kind"]
            path = art["path"]
            text = art["text"]
            question = f"[repo:{name}] {kind} · {path}"
            try:
                did = ws_dg.memory.store(
                    question=question[:500],
                    answer=text[:6000],
                    reasoning_summary=(
                        f"Ingested from repo {name}; kind={kind}; "
                        f"path={path}; bytes={len(text)}"),
                    communities_used=[], context_triples=[])
                stats[kind] = stats.get(kind, 0) + 1
                stats["stored"] += 1
                learnings_written.append(did)
            except Exception:
                stats["skipped"] += 1

    if include_commits:
        for c in _collect_recent_commits(repo_root):
            question = f"[repo:{name}] commit · {c['sha']} · {c['subject'][:120]}"
            answer = c["subject"]
            if c["body"]:
                answer += "\n\n" + c["body"]
            try:
                did = ws_dg.memory.store(
                    question=question[:500],
                    answer=answer[:4000],
                    reasoning_summary=(
                        f"Commit {c['sha']} by {c['author']} at {c['ts']} "
                        f"in repo {name}"),
                    communities_used=[], context_triples=[])
                stats["commit"] += 1
                stats["stored"] += 1
                learnings_written.append(did)
            except Exception:
                stats["skipped"] += 1

    try:
        ws_dg.memory.save()
    except Exception:
        pass

    return {**stats, "learnings_written": learnings_written}


def get_codebase_context(ws_dg, file_path: Optional[str] = None,
                          intent: Optional[str] = None,
                          repo_name: Optional[str] = None,
                          limit: int = 10) -> dict:
    """Return institutional context relevant to a file the user is editing.

    Pulls from `ws_dg.memory` (active decisions) matching:
      · repo-tagged decisions  ([repo:<repo>] ...)
      · path-related decisions (substring match on file path components)
      · intent-keyword matches against question/answer text

    Designed to be called by an IDE/agent right before answering a coding
    question, so the response is grounded in the project's history.
    """
    decisions = []
    try:
        decisions = ws_dg.memory.get_active_decisions()
    except Exception:
        return {"error": "could not read decisions"}

    fp = (file_path or "").lower()
    intent_l = (intent or "").lower()
    intent_words = {w for w in re.findall(r"[a-z0-9_]+", intent_l)
                    if len(w) > 3}
    # extract meaningful tokens from file path
    fp_words = set()
    if fp:
        for part in re.split(r"[\\/._\-]", fp):
            if part and len(part) > 2:
                fp_words.add(part.lower())

    scored = []
    for d in decisions:
        q = (d.get("question") or "").lower()
        a = (d.get("answer") or "").lower()
        score = 0
        if repo_name and f"[repo:{repo_name.lower()}]" in q:
            score += 5
        if fp and any(w in q or w in a for w in fp_words):
            score += 4
        if intent_words and any(w in q or w in a for w in intent_words):
            score += 3
        if "[adr" in q or "[repo:" in q and "adr" in q:
            score += 2
        if score > 0:
            scored.append((score, d))
    scored.sort(key=lambda x: (x[0], d.get("confidence", 0)), reverse=True)

    hits = []
    for score, d in scored[:limit]:
        hits.append({
            "id": d.get("id"),
            "question": d.get("question", "")[:200],
            "answer": (d.get("answer") or "")[:600],
            "score": score,
            "confidence": d.get("confidence", 0),
            "timestamp": d.get("timestamp", ""),
        })
    return {"file_path": file_path, "intent": intent,
            "repo_name": repo_name, "hits": hits,
            "context": _format_context_block(hits, file_path, intent)}


def _format_context_block(hits: list[dict], file_path: Optional[str],
                           intent: Optional[str]) -> str:
    """Return a ready-to-paste system-prompt context block."""
    if not hits:
        return ("No institutional context found for this file/intent — the "
                "DG memory has no prior decisions related to it.")
    lines = ["INSTITUTIONAL CONTEXT (from DecisionGraph memory):"]
    if file_path:
        lines.append(f"  Current file: {file_path}")
    if intent:
        lines.append(f"  Intent: {intent}")
    lines.append("")
    for i, h in enumerate(hits, 1):
        lines.append(f"  [{i}] {h['question']}")
        ans = h["answer"].replace("\n", " ")[:280]
        lines.append(f"      → {ans}")
    lines.append("")
    lines.append("Use this context when answering. Cite specific items by [N].")
    return "\n".join(lines)


# ── GitHub URL ingestion (v0 — shallow clone + reuse ingest_repo) ────────────
_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"
)

# Lightweight per-file code-summary chunks. Tier 1 of the repo-ingest feature:
# walk source files, summarise each with the LLM, store as decisions tagged
# `[repo:<name>] code · <path>` so they show up in normal DG retrieval.
_CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
             ".java", ".rb", ".php", ".c", ".cc", ".cpp", ".h",
             ".hpp", ".swift", ".kt", ".scala", ".sql"}
_CODE_MAX_BYTES   = 80_000          # skip giant generated / vendored files
_CODE_MAX_FILES   = 2000            # raised — structural pass has no LLM cost; semantic uses parallel workers
_CODE_MAX_CHUNKS  = 600             # raised proportionally — parallelized triple extraction
_CODE_SKIP_DIRS   = {"node_modules", "dist", "build", ".git", "vendor",
                      "__pycache__", ".next", ".venv", "venv", "target",
                      ".turbo", ".cache"}
# tests/examples/docs intentionally kept — they're real source files and benchmarks count them


def _parse_github_url(url: str) -> Optional[tuple[str, str]]:
    """Return (owner, repo) if the URL is a valid github.com URL, else None."""
    m = _GITHUB_URL_RE.match((url or "").strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    return owner, repo


def _collect_code_files(repo_root: Path) -> list[dict]:
    """Walk the repo for source files we'd want one-shot LLM summaries of."""
    items: list[dict] = []
    for root, dirs, files in os.walk(repo_root):
        # prune skip-dirs in-place so os.walk doesn't descend into them
        dirs[:] = [d for d in dirs if d not in _CODE_SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _CODE_EXT:
                continue
            full = Path(root) / fn
            try:
                size = full.stat().st_size
                if size == 0 or size > _CODE_MAX_BYTES:
                    continue
            except OSError:
                continue
            rel = str(full.relative_to(repo_root)).replace("\\", "/")
            text = _safe_read(full, _CODE_MAX_BYTES)
            if not text:
                continue
            items.append({"path": rel, "text": text, "ext": ext})
            if len(items) >= _CODE_MAX_FILES:
                return items
    return items


def _llm_summarise_file(client, model: str, path: str, code: str) -> str:
    """One LLM call → 2-3 sentence plain-English summary of what this file does."""
    prompt = (
        f"You are summarising one source file for an institutional memory "
        f"graph. In 2-3 sentences, plain English, explain what this file does "
        f"and its key responsibilities. No code, no markdown, no preamble.\n\n"
        f"PATH: {path}\n\nCODE:\n{code[:6000]}\n\nSUMMARY:")
    try:
        r = client.messages.create(
            model=model, max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(getattr(b, "text", "") for b in r.content).strip()
    except Exception as e:
        return f"(summary failed: {e})"


def _load_repo_cache(ws_dg, repo_url: str) -> dict:
    """Per-workspace incremental-ingest cache. Keyed by repo URL.
    Stores {file_path: sha1} so subsequent ingests skip unchanged files."""
    import json as _json
    cache_dir = Path(ws_dg.storage_dir) / "github_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", repo_url)[:120]
    path = cache_dir / f"{safe}.json"
    if path.exists():
        try:
            return _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_repo_cache(ws_dg, repo_url: str, cache: dict) -> None:
    import json as _json
    cache_dir = Path(ws_dg.storage_dir) / "github_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", repo_url)[:120]
    (cache_dir / f"{safe}.json").write_text(
        _json.dumps(cache, indent=2), encoding="utf-8")


def _fetch_recent_prs(owner: str, repo: str, max_prs: int = 10) -> list[dict]:
    """Best-effort: pull recent closed PRs via GitHub's public API.
    Unauthenticated (rate-limited to 60/hr per IP). Returns [] on any error."""
    import urllib.request, json as _json
    url = (f"https://api.github.com/repos/{owner}/{repo}/pulls"
            f"?state=closed&per_page={max_prs}&sort=updated&direction=desc")
    try:
        req = urllib.request.Request(url, headers={
            "Accept":     "application/vnd.github+json",
            "User-Agent": "DecisionGraph-ingest"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        out = []
        for pr in data:
            out.append({
                "number":   pr.get("number"),
                "title":    pr.get("title", "")[:200],
                "body":     (pr.get("body") or "")[:2000],
                "author":   (pr.get("user") or {}).get("login", ""),
                "merged":   pr.get("merged_at") is not None,
                "url":      pr.get("html_url", ""),
                "created":  pr.get("created_at", ""),
            })
        return out
    except Exception:
        return []


def _build_repo_blueprint(ws_dg, clone_path: str, owner: str, repo: str,
                            progress_cb=None) -> str:
    """Walk the cloned repo and emit ONE big Markdown 'blueprint' that mirrors
    the codebase as structured prose. This blueprint becomes a document we
    feed through DG's normal ingest() pipeline — so DG's existing triple
    extraction, Louvain community detection, and compiled-summary machinery
    do their normal work on it. No parallel system.

    The prose intentionally uses natural language for relationships
    ('implements', 'calls', 'lives in folder') so DG's triple extractor
    picks them up as edges automatically."""
    import os as _os
    from pathlib import Path as _Path
    from . import config as _cfg
    model = _cfg.LLM_MODEL or "gemini-3-flash"
    client = ws_dg.client

    out: list[str] = []

    # ── PART 1: project header (mission + stack + recent activity) ───────
    if progress_cb: progress_cb("reading README + manifest", 0.05)
    out.append(f"# Project: {owner}/{repo}\n")

    # README → mission
    readme_path = None
    for cand in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        p = _os.path.join(clone_path, cand)
        if _os.path.isfile(p):
            readme_path = p; break
    if readme_path:
        readme = _safe_read(_Path(readme_path), max_bytes=30_000) or ""
        # ask LLM to extract a mission paragraph + named concepts from README
        try:
            prompt = (
                f"Read this README of `{owner}/{repo}` and produce TWO things, "
                f"separated by '---'. "
                f"FIRST: one paragraph (3-5 sentences) describing what this "
                f"project is and what it does — be specific, cite actual feature "
                f"names from the README. "
                f"SECOND: a comma-separated list of 4-8 named concepts/features "
                f"this project implements (e.g. 'spaced repetition, OAuth login, "
                f"daily streak gamification'). Just the list, no numbering.\n\n"
                f"README:\n{readme[:8000]}\n\n"
                f"PARAGRAPH AND CONCEPTS:")
            r = client.messages.create(model=model, max_tokens=2000,
                messages=[{"role": "user", "content": prompt}])
            block = "".join(getattr(b, "text", "") for b in r.content).strip()
        except Exception as e:
            block = f"(README summary failed: {e})"
        parts = block.split("---", 1) if "---" in block else (block, "")
        mission = parts[0].strip()
        concepts_text = parts[1].strip() if len(parts) > 1 else ""
        out.append("## Mission\n")
        out.append(mission + "\n")
        if concepts_text:
            # parse comma-separated concepts
            concepts = [c.strip().rstrip(".").strip() for c in concepts_text.split(",")]
            concepts = [c for c in concepts if c and 2 < len(c) < 100][:10]
            if concepts:
                out.append("## Key Concepts\n")
                out.append(f"The `{repo}` project implements the following named concepts:\n")
                for c in concepts:
                    out.append(f"- The `{repo}` project implements **{c}**.")
                out.append("")
                # remember concepts for later linking
                ws_dg._tmp_concepts = concepts   # used by file-section pass below
            else:
                ws_dg._tmp_concepts = []
        else:
            ws_dg._tmp_concepts = []
    else:
        ws_dg._tmp_concepts = []

    # manifest → stack
    for manifest in ("package.json", "pyproject.toml", "Cargo.toml", "go.mod",
                       "requirements.txt", "Gemfile", "pom.xml"):
        p = _os.path.join(clone_path, manifest)
        if _os.path.isfile(p):
            mtext = _safe_read(_Path(p), max_bytes=10_000) or ""
            if mtext:
                out.append(f"\n## Stack (from {manifest})\n")
                out.append("```\n" + mtext[:2000] + "\n```\n")
            break

    # ── PART 2: file-level structured prose ──────────────────────────────
    if progress_cb: progress_cb("scanning source files", 0.15)
    code_files = _collect_code_files(_Path(clone_path))
    file_count = min(len(code_files), _CODE_MAX_FILES)

    out.append(f"\n# Files in {owner}/{repo}\n")
    out.append(
        f"The `{repo}` codebase contains {file_count} key source files. "
        f"Each file lives in a folder and contains functions or classes that "
        f"call each other.\n")

    # build file-role + functions per file. File-role LLM calls are PARALLEL
    # (up to 10 in flight) — the sequential version took ~5 min for 60 files;
    # parallel takes ~30 seconds.
    from .codebase_ast import parse_file_ast
    import concurrent.futures as _cf
    import re as _re_fr

    def _file_role(art):
        path = art["path"]
        try:
            r = client.messages.create(
                model=model, max_tokens=600,
                messages=[{"role": "user", "content":
                    f"In ONE sentence (max 30 words), describe what the file "
                    f"`{path}` does in the `{repo}` codebase. Output the "
                    f"sentence directly, no preamble.\n\n{art['text'][:5000]}"
                    f"\n\nROLE:"}])
            role = "".join(getattr(b, "text", "") for b in r.content).strip()
            role = _re_fr.sub(r"^ROLE:\s*", "", role, flags=_re_fr.I)
            role = role.strip().strip('"').strip("'")
        except Exception as e:
            role = f"(file role failed: {e})"
        return path, role, art

    if progress_cb:
        progress_cb(f"summarising {file_count} files in parallel", 0.20)

    files_to_process = code_files[:_CODE_MAX_FILES]
    role_results: dict[str, tuple[str, dict]] = {}
    files_done = 0
    with _cf.ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_file_role, art) for art in files_to_process]
        for fut in _cf.as_completed(futures):
            try:
                path, role, art = fut.result()
                role_results[path] = (role, art)
            except Exception as e:
                pass
            files_done += 1
            if progress_cb and files_done % 5 == 0:
                progress_cb(
                    f"file roles: {files_done}/{file_count}",
                    0.20 + 0.45 * (files_done / max(1, file_count)))

    # now write the blueprint in deterministic order (original file order)
    for art in files_to_process:
        path = art["path"]
        if path not in role_results:
            continue
        role, _ = role_results[path]
        folder = _os.path.dirname(path) or "."

        out.append(f"\n## File: {path}\n")
        out.append(f"The file `{path}` lives in folder `{folder}/` "
                    f"of `{owner}/{repo}`. {role}\n")

        # link to any concept name that appears in the file's text or role
        for concept in (getattr(ws_dg, "_tmp_concepts", []) or []):
            c_low = concept.lower()
            if c_low in (role + " " + art["text"][:3000]).lower():
                out.append(f"The file `{path}` implements **{concept}**.")
        out.append("")

        # AST-chunk this file → function names + call relationships only
        # (NO per-function LLM call — saves ~120 LLM calls for a typical repo).
        chunks = parse_file_ast(path, art["text"]) or []
        if chunks:
            func_list = ", ".join(
                f"`{ch['name']}` ({ch['kind'].replace('_definition','').replace('_declaration','')})"
                for ch in chunks[:25])
            out.append(f"The file `{path}` defines: {func_list}.")
            for ch in chunks[:30]:
                callees = (ch.get("calls") or [])[:4]
                for callee in callees:
                    out.append(f"In `{path}`, `{ch['name']}` calls `{callee}`.")

    # ── PART 3: recent commits summary ───────────────────────────────────
    if progress_cb: progress_cb("reading git history", 0.88)
    commits = _collect_recent_commits(_Path(clone_path), n=20)
    if commits:
        out.append(f"\n# Recent activity in {owner}/{repo}\n")
        out.append(f"The most recent commits to `{repo}`:\n")
        for c in commits[:20]:
            out.append(f"- {c['author']} committed: {c['subject'][:160]}")

    # ── PART 4: PRs (already fetched separately, but include here too) ────
    if progress_cb: progress_cb("fetching pull requests", 0.93)
    prs = _fetch_recent_prs(owner, repo, max_prs=10)
    if prs:
        out.append(f"\n# Recent pull requests in {owner}/{repo}\n")
        for pr in prs:
            out.append(
                f"- PR #{pr['number']} by {pr['author']}: {pr['title'][:160]}")

    # cleanup
    try: delattr(ws_dg, "_tmp_concepts")
    except Exception: pass

    return "\n".join(out)


def ingest_github_url_v2(ws_dg, repo_url: str,
                          branch: Optional[str] = None,
                          progress_cb=None) -> dict:
    """The CORRECT path. Shallow-clone the repo, build ONE structured blueprint
    document, then feed it through DG's existing ingest() pipeline. DG handles
    triples, communities, compiled summaries, embeddings — all of it — using
    the same machinery as for PDFs. No parallel /api/repo/ask needed."""
    import tempfile, shutil
    parsed = _parse_github_url(repo_url)
    if not parsed:
        return {"error": f"not a valid github.com URL: {repo_url!r}"}
    owner, repo = parsed

    tmp_parent = tempfile.mkdtemp(prefix="dg_gh2_")
    clone_path = os.path.join(tmp_parent, repo)
    try:
        cmd = ["git", "clone", "--depth", "1"]
        if branch: cmd += ["--branch", branch, "--single-branch"]
        cmd += [repo_url, clone_path]
        if progress_cb: progress_cb("cloning", 0.02)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                                encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return {"error": f"git clone failed: {proc.stderr.strip()[:400]}"}

        # build the blueprint markdown
        blueprint = _build_repo_blueprint(
            ws_dg, clone_path, owner, repo, progress_cb=progress_cb)
        if not blueprint or blueprint.count("\n") < 5:
            return {"error": "blueprint generation produced empty output"}

        # write to a temp file in the workspace's storage dir so it persists
        bp_dir = os.path.join(ws_dg.storage_dir, "blueprints")
        os.makedirs(bp_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{owner}_{repo}")[:120]
        bp_path = os.path.join(bp_dir, f"{safe}.md")
        with open(bp_path, "w", encoding="utf-8") as f:
            f.write(blueprint)

        # ── In parallel with DG semantic ingest, build a SQLite structural
        # graph (tree-sitter only, no LLM). Adds ~0.5s for small repos, a few
        # seconds for big ones. Enables fast "what calls X" / "blast radius"
        # queries that the semantic path can't do.
        try:
            from .code_graph import build_from_repo as _build_cg
            cg_db = os.path.join(ws_dg.storage_dir, "code_graph.db")
            cg_stats = _build_cg(cg_db, clone_path, f"{owner}/{repo}",
                                   progress_cb=progress_cb)
            print(f"  [code_graph] {cg_stats}")
        except Exception as e:
            print(f"  [code_graph] skipped: {e}")
            cg_stats = {"error": str(e)}

        # feed blueprint through DG's existing ingest pipeline — same path as PDFs
        if progress_cb: progress_cb("ingesting blueprint into DG (triples, communities, compiled summaries)", 0.96)
        ws_dg.ingest(bp_path)
        try: ws_dg.memory.save()
        except Exception: pass

        if progress_cb: progress_cb("done", 1.0)
        return {
            "repo":             f"{owner}/{repo}",
            "owner":            owner,
            "blueprint_path":   bp_path,
            "blueprint_chars":  len(blueprint),
            "nodes":            ws_dg.G.number_of_nodes() if ws_dg.G is not None else 0,
            "edges":            ws_dg.G.number_of_edges() if ws_dg.G is not None else 0,
            "communities":      len(ws_dg.communities or {}),
            "branch":           branch or "default",
            # structural graph stats (separate SQLite DB, queryable via MCP tools)
            "code_graph":       cg_stats,
        }
    finally:
        try: shutil.rmtree(tmp_parent, ignore_errors=True)
        except Exception: pass


def ingest_github_url(ws_dg, repo_url: str,
                       branch: Optional[str] = None,
                       include_code_summaries: bool = True,
                       use_ast: bool = True,
                       include_call_edges: bool = True,
                       include_hierarchical: bool = True,
                       include_prs: bool = True,
                       incremental: bool = True,
                       progress_cb=None) -> dict:
    """Shallow-clone a github.com repo and ingest it into the worker's DG.

    Layers (each toggleable):
      1. Docs / ADRs / manifests / commits          (always — via ingest_repo)
      2. Per-chunk code summaries via tree-sitter   (use_ast=True, default)
         falls back to per-file summary if tree-sitter doesn't support the lang
      3. Call-graph edges as graph triples          (include_call_edges)
      4. Folder + repo hierarchical summaries       (include_hierarchical)
      5. Recent closed PRs via GitHub public API    (include_prs)
      6. Incremental: skip files whose hash hasn't changed since last ingest
                                                     (incremental=True)
    """
    import tempfile, shutil
    parsed = _parse_github_url(repo_url)
    if not parsed:
        return {"error": f"not a valid github.com URL: {repo_url!r}"}
    owner, repo = parsed

    tmp_parent = tempfile.mkdtemp(prefix="dg_gh_")
    clone_path = os.path.join(tmp_parent, repo)
    try:
        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd += ["--branch", branch, "--single-branch"]
        cmd += [repo_url, clone_path]
        if progress_cb: progress_cb("cloning", 0.05)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                                encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return {"error": f"git clone failed: {proc.stderr.strip()[:400]}"}

        # ── Layer 1: docs / ADRs / manifests / commits (existing) ────────────
        if progress_cb: progress_cb("reading docs + commits", 0.15)
        stats = ingest_repo(ws_dg, clone_path,
                              include_files=True, include_commits=True)
        if "error" in stats:
            return stats

        # ── incremental cache: hash all source files, skip unchanged ─────────
        cache = _load_repo_cache(ws_dg, repo_url) if incremental else {}
        new_hashes: dict[str, str] = {}
        code_files = _collect_code_files(Path(clone_path))

        # compute current hashes
        try:
            from .codebase_ast import compute_file_hash
            for art in code_files:
                full = Path(clone_path) / art["path"]
                new_hashes[art["path"]] = compute_file_hash(full)
        except Exception:
            new_hashes = {a["path"]: "" for a in code_files}

        # filter to only changed files (or all if no cache)
        if incremental and cache:
            changed_paths = {
                p for p, h in new_hashes.items()
                if cache.get(p) != h}
            code_files = [a for a in code_files if a["path"] in changed_paths]
            stats["incremental_skipped"] = len(new_hashes) - len(code_files)
        else:
            stats["incremental_skipped"] = 0

        # ── Layer 2: per-chunk summaries via tree-sitter (or fallback) ───────
        code_summaries  = 0
        chunk_summaries = 0
        all_chunks: list[dict] = []
        if include_code_summaries:
            try:
                from . import config
                from .codebase_ast import (
                    parse_file_ast, summarise_chunk, summarise_folder,
                    summarise_repo, build_call_edges)
                model = config.LLM_MODEL or "gemini-3-flash"
                client = ws_dg.client
                if progress_cb:
                    progress_cb(f"processing {len(code_files)} code files", 0.25)

                chunks_so_far = 0
                for i, art in enumerate(code_files):
                    if chunks_so_far >= _CODE_MAX_CHUNKS:
                        break  # hit overall chunk cap — stop summarising
                    chunks = []
                    if use_ast:
                        chunks = parse_file_ast(art["path"], art["text"])
                        # respect global cap across files
                        chunks = chunks[: max(0, _CODE_MAX_CHUNKS - chunks_so_far)]
                    if chunks:
                        # AST path: one DG entry per function/class chunk
                        for ch in chunks:
                            summ = summarise_chunk(client, model, ch)
                            try:
                                # Make the QUESTION semantically rich so embedding
                                # retrieval actually finds it. Include repo name,
                                # kind, function/class name, and file path — all
                                # phrased as a real sentence.
                                kind_word = ch["kind"].replace("_", " ")
                                rich_q = (f"What does the {ch['name']} {kind_word} "
                                            f"do in the {repo} codebase? "
                                            f"(file: {ch['path']}, "
                                            f"owner: {owner}, lang: {ch['lang']})")
                                ws_dg.memory.store(
                                    question=rich_q[:500],
                                    answer=summ[:2000],
                                    reasoning_summary=(
                                        f"AST chunk · repo={owner}/{repo} · "
                                        f"file={ch['path']} · lines "
                                        f"{ch['start_line']}-{ch['end_line']} · "
                                        f"name={ch['name']} · lang={ch['lang']}"),
                                    communities_used=[], context_triples=[])
                                chunk_summaries += 1
                            except Exception:
                                pass
                        all_chunks.extend(chunks)
                        chunks_so_far += len(chunks)
                    else:
                        # Fallback path: whole-file summary
                        summary = _llm_summarise_file(
                            client, model, art["path"], art["text"])
                        try:
                            rich_q = (f"What does the file {art['path']} do in "
                                        f"the {repo} codebase? "
                                        f"(owner: {owner})")
                            ws_dg.memory.store(
                                question=rich_q[:500],
                                answer=summary[:4000],
                                reasoning_summary=(
                                    f"File summary · repo={owner}/{repo} · "
                                    f"file={art['path']} · lang={art['ext']} · "
                                    f"bytes={len(art['text'])}"),
                                communities_used=[], context_triples=[])
                            code_summaries += 1
                        except Exception:
                            pass
                    if progress_cb and i % 4 == 0:
                        progress_cb(
                            f"processed {i+1}/{len(code_files)}",
                            0.25 + 0.45 * (i / max(1, len(code_files))))

                # ── Layer 3: call-graph edges → graph triples ────────────────
                edges_added = 0
                if include_call_edges and all_chunks:
                    edges = build_call_edges(all_chunks)
                    # lazy-init graph if it doesn't exist yet
                    if not hasattr(ws_dg, "G") or ws_dg.G is None:
                        try:
                            import networkx as nx
                            ws_dg.G = nx.MultiDiGraph()
                        except Exception:
                            ws_dg.G = None
                    if ws_dg.G is not None:
                        for e in edges[:1000]:   # cap to keep graph readable
                            try:
                                ws_dg.G.add_edge(
                                    e["src"], e["dst"],
                                    label="calls",
                                    src_path=e["src_path"],
                                    dst_path=e["dst_path"],
                                    repo=f"{owner}/{repo}")
                                edges_added += 1
                            except Exception:
                                pass
                        # persist the graph so the next query sees the edges
                        try:
                            from .graph import save_graph_state
                            save_graph_state(
                                ws_dg.G,
                                getattr(ws_dg, "communities", None),
                                getattr(ws_dg, "summaries", None),
                                save_dir=ws_dg.storage_dir)
                        except Exception as e:
                            stats["graph_save_error"] = str(e)
                    stats["call_edges"] = edges_added

                # ── Layer 4: hierarchical summaries (folder + repo) ──────────
                folder_summaries: dict[str, str] = {}
                repo_summary = ""
                if include_hierarchical and all_chunks:
                    if progress_cb:
                        progress_cb("rolling up folder summaries", 0.75)
                    # group chunks by parent folder; aggregate names+summaries
                    by_folder: dict[str, list[str]] = {}
                    for ch in all_chunks:
                        folder = os.path.dirname(ch["path"]) or "."
                        by_folder.setdefault(folder, []).append(
                            f"{ch['name']} ({ch['kind']})")
                    for folder, names in list(by_folder.items())[:30]:
                        folder_summary = summarise_folder(
                            client, model, folder, names)
                        folder_summaries[folder] = folder_summary
                        try:
                            rich_q = (f"What is in the {folder} folder of the "
                                        f"{repo} codebase? What is its role? "
                                        f"(owner: {owner})")
                            ws_dg.memory.store(
                                question=rich_q[:500],
                                answer=folder_summary[:2000],
                                reasoning_summary=(
                                    f"Folder roll-up · repo={owner}/{repo} · "
                                    f"folder={folder} · {len(names)} items"),
                                communities_used=[], context_triples=[])
                        except Exception:
                            pass
                    # repo-level summary
                    if folder_summaries:
                        if progress_cb: progress_cb("rolling up repo summary", 0.85)
                        repo_summary = summarise_repo(
                            client, model, f"{owner}/{repo}", folder_summaries)
                        try:
                            # The MOST IMPORTANT entry for retrieval — when the
                            # user asks "what is {repo}?" or "what does {repo} do?",
                            # this is what should match. Pack multiple phrasings
                            # into the question text so embeddings hit it.
                            rich_q = (
                                f"What is the {repo} project? What does the "
                                f"{repo} codebase do? Overview of {owner}/{repo}. "
                                f"What is {repo}? Describe the {repo} repository.")
                            ws_dg.memory.store(
                                question=rich_q[:500],
                                answer=repo_summary[:3000],
                                reasoning_summary=(
                                    f"Repo overview · repo={owner}/{repo} · "
                                    f"built from {len(folder_summaries)} folder rollups"),
                                communities_used=[], context_triples=[])
                        except Exception:
                            pass
                    stats["folder_summaries"] = len(folder_summaries)
                    stats["repo_summary"]     = bool(repo_summary)

                try: ws_dg.memory.save()
                except Exception: pass
            except Exception as e:
                stats["code_summary_error"] = str(e)
                import traceback; traceback.print_exc()

        # ── Layer 5: recent PRs via GitHub public API ────────────────────────
        prs_stored = 0
        if include_prs:
            if progress_cb: progress_cb("fetching recent PRs", 0.92)
            for pr in _fetch_recent_prs(owner, repo, max_prs=10):
                body = pr["title"]
                if pr["body"]:
                    body += "\n\n" + pr["body"]
                try:
                    rich_q = (f"PR #{pr['number']} in {repo}: {pr['title'][:160]}"
                                f" (by {pr['author']} in {owner}/{repo})")
                    ws_dg.memory.store(
                        question=rich_q[:500],
                        answer=body[:3000],
                        reasoning_summary=(
                            f"PR #{pr['number']} by {pr['author']} "
                            f"({'merged' if pr['merged'] else 'closed'}) "
                            f"in {owner}/{repo} · {pr['url']}"),
                        communities_used=[], context_triples=[])
                    prs_stored += 1
                except Exception:
                    pass
            stats["prs"] = prs_stored
            try: ws_dg.memory.save()
            except Exception: pass

        # ── persist hash cache for next incremental run ──────────────────────
        if incremental:
            # merge: keep old entries for files we skipped (unchanged), update new ones
            merged = {**cache, **new_hashes}
            _save_repo_cache(ws_dg, repo_url, merged)

        stats["owner"]           = owner
        stats["repo_url"]        = repo_url
        stats["branch"]          = branch or "default"
        stats["chunk_summaries"] = chunk_summaries
        stats["code_summaries"]  = code_summaries
        if progress_cb: progress_cb("done", 1.0)
        return stats
    finally:
        try:
            shutil.rmtree(tmp_parent, ignore_errors=True)
        except Exception:
            pass
