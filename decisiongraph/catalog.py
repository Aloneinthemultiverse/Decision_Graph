"""Read the ECC TASK CATALOG (permanent-memory menu) and, for a given task,
select the relevant assets the dispatcher should pull in:

  * persona  — best matching agent (1)
  * skills   — top-K how-to skills by topic similarity (0..many)
  * rules    — every rule whose `[applies to: <globs>]` matches the task files

The catalog is a single markdown file produced by gen_catalog.py. We parse its
`## Section (n)` blocks and `- **name** — description` lines. Matching is plain
keyword overlap by default; if an embed model is passed we use cosine instead.
This is the "read the menu, then choose" step the kernel's LOAD can call.
"""
from __future__ import annotations
import os, re, fnmatch, json

_SECTION = re.compile(r"^##\s+(.+?)\s*\(\d+\)\s*$")
_ITEM = re.compile(r"^- \*\*(?P<name>[^*]+)\*\*\s+—\s+(?P<desc>.+)$")
_APPLIES = re.compile(r"\[applies to:\s*(?P<globs>[^\]]+)\]")
_STOP = set("the a an and or to of in on for with when use this that is are be by "
            "as at from your you it its which who whom into only build implement "
            "write create code file files project layer using small".split())


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+]{2,}", s.lower())
            if w not in _STOP}


def load_catalog(path: str) -> dict[str, list[dict]]:
    """Parse TASK_CATALOG.md → {section_name: [{name, desc, globs?}]}."""
    sections: dict[str, list[dict]] = {}
    cur = None
    if not os.path.exists(path):
        return sections
    for line in open(path, encoding="utf-8"):
        ms = _SECTION.match(line.strip())
        if ms:
            cur = ms.group(1)
            sections[cur] = []
            continue
        mi = _ITEM.match(line.rstrip())
        if mi and cur is not None:
            name, desc = mi.group("name").strip(), mi.group("desc").strip()
            entry = {"name": name, "desc": desc, "_words": _words(name + " " + desc)}
            mg = _APPLIES.search(desc)
            if mg:
                entry["globs"] = [g.strip() for g in mg.group("globs").split(",")
                                  if g.strip() and g.strip() != "--"]
            sections[cur].append(entry)
    return sections


def _rank(items, task, embed_model=None, top_k=3, min_score=1):
    """Rank catalog items against the task; return up to top_k above threshold."""
    if not items:
        return []
    if embed_model is not None:
        try:
            import numpy as np
            tv = embed_model.encode([task])[0]
            iv = embed_model.encode([f"{it['name']}. {it['desc']}" for it in items])
            sims = (iv @ tv) / ((np.linalg.norm(iv, axis=1) * np.linalg.norm(tv)) + 1e-9)
            order = sims.argsort()[::-1][:top_k]
            return [(items[i], float(sims[i])) for i in order if sims[i] > 0.15]
        except Exception:
            pass
    tw = _words(task)
    scored = sorted(((len(tw & it["_words"]), it) for it in items),
                    key=lambda x: x[0], reverse=True)
    return [(it, float(s)) for s, it in scored[:top_k] if s >= min_score]


# ── task → (languages, domains) signal extraction ────────────────────────────
_EXT_LANG = {
    ".py": ["python"], ".js": ["javascript", "frontend"], ".ts": ["typescript"],
    ".tsx": ["typescript", "react", "frontend"], ".jsx": ["react", "frontend"],
    ".html": ["frontend", "html", "web"], ".css": ["frontend", "css", "web"],
    ".go": ["golang"], ".rs": ["rust"], ".java": ["java"], ".kt": ["kotlin"],
    ".swift": ["swift"], ".cpp": ["cpp"], ".cs": ["csharp"], ".rb": ["ruby"],
}
_DOMAIN_HINTS = {
    "frontend": ["ui", "browser", "page", "css", "html", "landing", "client-side",
                 "localstorage", "design", "frontend", "web", "style"],
    "backend":  ["api", "flask", "route", "server", "endpoint", "service",
                 "fastapi", "django", "backend"],
    "data":     ["store", "database", "schema", "model", "data layer", "crud",
                 "in-memory", "dict store", "persistence"],
    "testing":  ["test", "tests", "pytest", "tdd", "coverage", "unit"],
    "docs":     ["readme", "documentation", "scaffold", "requirements"],
}


def task_signals(task: str, task_files: list[str] | None = None) -> set[str]:
    """Derive language + domain tokens from the task text and target files."""
    sig: set[str] = set()
    tl = task.lower()
    for f in (task_files or []):
        ext = os.path.splitext(f)[1].lower()
        sig.update(_EXT_LANG.get(ext, []))
    for domain, hints in _DOMAIN_HINTS.items():
        if any(h in tl for h in hints):
            sig.add(domain)
    return sig


# every language token a skill name might carry, + framework→language mapping
_ALL_LANGS = {"python", "javascript", "typescript", "react", "golang", "go",
              "rust", "java", "kotlin", "swift", "cpp", "csharp", "fsharp",
              "ruby", "dart", "flutter", "perl", "php", "scala", "elixir"}
_FRAMEWORK_LANG = {
    "django": "python", "fastapi": "python", "flask": "python", "pytorch": "python",
    "react": "javascript", "nextjs": "javascript", "nuxt": "javascript",
    "angular": "typescript", "vue": "javascript", "nestjs": "typescript",
    "springboot": "java", "quarkus": "java", "jpa": "java",
    "laravel": "php", "rails": "ruby", "ktor": "kotlin", "exposed": "kotlin",
    "swiftui": "swift", "compose": "kotlin",
}


def _conflicts_language(name_tok: set[str], langs: set[str]) -> bool:
    """True if the skill name names a language/framework that ISN'T the task's
    language. Lets a Python task drop 'cpp-testing', 'csharp-testing', etc."""
    if not langs:
        return False
    for tok in name_tok:
        if tok in _ALL_LANGS and tok not in langs:
            # treat go/golang as same
            if not (tok in ("go", "golang") and langs & {"go", "golang"}):
                return True
        if tok in _FRAMEWORK_LANG and _FRAMEWORK_LANG[tok] not in langs:
            return True
    return False


def _rank_skills(items, task, task_files, top_k=3):
    """Two-stage skill ranking: bias hard toward skills whose NAME/desc carry the
    task's language + domain signals, and DROP skills naming a conflicting
    language. A Python UI task gets frontend/python skills — not 'cpp-testing'."""
    if not items:
        return []
    sig = task_signals(task, task_files)
    langs = {s for s in sig if s in _ALL_LANGS}
    tw = _words(task) | sig
    scored = []
    for it in items:
        name_tok = _words(it["name"])
        if _conflicts_language(name_tok, langs):
            continue                         # wrong-language skill → exclude
        desc_tok = it["_words"]
        score = 3 * len(tw & name_tok) + 1 * len(tw & desc_tok)
        score += 4 * len(sig & name_tok) + 2 * len(sig & desc_tok)
        # small reward for naming the task's own language explicitly
        score += 5 * len(langs & name_tok)
        if score > 0:
            scored.append((score, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(it, float(s)) for s, it in scored[:top_k]]


def select_for_task(catalog: dict, task: str, task_files: list[str] | None = None,
                    embed_model=None) -> dict:
    """Read the menu and choose persona + skills + rules for this task."""
    task_files = task_files or []
    out = {"persona": None, "skills": [], "rules": []}

    personas = catalog.get("Personas (agents)", [])
    p = _rank(personas, task, embed_model, top_k=1, min_score=1)
    if p:
        out["persona"] = p[0][0]["name"]

    skills = catalog.get("Skills", [])
    out["skills"] = [it["name"] for it, _ in
                     _rank_skills(skills, task, task_files, top_k=3)]

    # rules: deterministic — file-path glob match, no AI
    for r in catalog.get("Rules", []):
        globs = r.get("globs") or []
        if any(fnmatch.fnmatch(tf, g) or fnmatch.fnmatch(os.path.basename(tf), g)
               for tf in task_files for g in globs):
            out["rules"].append(r["name"])
    return out


def _strip_frontmatter(txt: str) -> str:
    return re.sub(r"^---\s*\n.*?\n---\s*\n", "", txt, count=1, flags=re.DOTALL).strip()


def skill_body(ecc_dir: str, name: str, limit: int = 1500) -> str:
    """Load a skill's actual SKILL.md know-how (trimmed) so the builder applies
    real guidance, not a one-line blurb."""
    p = os.path.join(ecc_dir, "skills", name, "SKILL.md")
    if not os.path.exists(p):
        return ""
    body = _strip_frontmatter(open(p, encoding="utf-8").read())
    return body[:limit].strip()


def build_persona_block(agents_by_name: dict, names: list[str]) -> str:
    """The persona the builder embodies (full body of the FIRST valid agent)."""
    for n in names or []:
        a = agents_by_name.get(n)
        if a and a.get("body"):
            return f"# You ARE the `{n}` agent. Work strictly in this role:\n{a['body']}"
    return ""


def build_skills_block(ecc_dir: str, names: list[str]) -> str:
    """The know-how the builder applies — real SKILL.md content for each skill."""
    chunks = []
    for n in names or []:
        b = skill_body(ecc_dir, n)
        if b:
            chunks.append(f"## SKILL: {n}\n{b}")
    if not chunks:
        return ""
    return ("# Apply the know-how from these ECC skills (guidance, not code to "
            "import):\n" + "\n\n".join(chunks))


def render_selection(sel: dict, catalog: dict) -> str:
    """Turn a selection into a markdown block to inject into the agent prompt."""
    def desc(section, name):
        for it in catalog.get(section, []):
            if it["name"] == name:
                return it["desc"]
        return ""
    lines = []
    if sel.get("skills"):
        lines.append("# Relevant ECC skills (apply this know-how)")
        for s in sel["skills"]:
            lines.append(f"- {s}: {desc('Skills', s)}")
    if sel.get("rules"):
        lines.append("\n# Rules that apply to these files (follow them)")
        for r in sel["rules"]:
            lines.append(f"- {r}: {desc('Rules', r)}")
    return "\n".join(lines)
