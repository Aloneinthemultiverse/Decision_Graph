"""Generate the ECC TASK CATALOG — a single markdown 'menu' of every ECC asset
with a one-line description each. This file is the permanent-memory catalog the
dispatcher reads to pick personas + skills + rules + commands for a task.

Run:  python gen_catalog.py   ->  writes ecc/TASK_CATALOG.md
"""
from __future__ import annotations
import os, re, json, glob

ECC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecc")
OUT = os.path.join(ECC, "TASK_CATALOG.md")

_FM = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def frontmatter(path):
    try:
        txt = open(path, encoding="utf-8").read()
    except Exception:
        return {}, ""
    m = _FM.search(txt)
    body = txt[m.end():] if m else txt
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "-", "\t")):
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, body


def first_heading(body):
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
    return ""


def first_sentence(body, limit=140):
    # first non-empty, non-heading, non-quote prose line
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ">", "-", "*", "|", "```", "<!--")):
            continue
        s = re.sub(r"\s+", " ", s)
        return (s[:limit] + "…") if len(s) > limit else s
    return ""


def oneline(path):
    """Best one-liner for a markdown asset: description frontmatter > first
    sentence > heading. Squashed to a single clean line."""
    fm, body = frontmatter(path)
    d = fm.get("description") or first_sentence(body) or first_heading(body) \
        or os.path.basename(path)
    d = re.sub(r"\s+", " ", d).strip()
    # strip trigger-phrase boilerplate so the menu reads as pure capability
    d = re.split(r"(?i)\s*(?:use proactively|must be used|use this agent|"
                 r"use immediately|invoke for|triggered by|use when)\b", d)[0]
    return d.strip().rstrip(".").strip()


def section(title, items):
    out = [f"\n## {title} ({len(items)})\n"]
    for name, desc in items:
        out.append(f"- **{name}** — {desc}")
    return "\n".join(out) + "\n"


def collect_md_dir(folder, pattern="*.md", name_from="stem"):
    items = []
    for p in sorted(glob.glob(os.path.join(folder, pattern))):
        stem = os.path.basename(p)[:-3]
        items.append((stem, oneline(p)))
    return items


def collect_skills():
    items = []
    for d in sorted(os.listdir(os.path.join(ECC, "skills"))):
        sk = os.path.join(ECC, "skills", d, "SKILL.md")
        if os.path.exists(sk):
            items.append((d, oneline(sk)))
    return items


def collect_rules():
    items = []
    base = os.path.join(ECC, "rules")
    for p in sorted(glob.glob(os.path.join(base, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(p, base)[:-3].replace("\\", "/")
        if rel.lower() == "readme":
            continue
        fm, body = frontmatter(p)
        paths = ""
        # rules carry a 'paths:' list under frontmatter — capture a hint
        txt = open(p, encoding="utf-8").read()
        mp = re.search(r"paths:\s*\n((?:\s*-\s*.*\n)+)", txt)
        if mp:
            globs = re.findall(r"-\s*\"?([^\"\n]+)\"?", mp.group(1))
            paths = ", ".join(g.strip() for g in globs[:4])
        title = first_heading(body) or rel
        desc = title
        if paths:
            desc += f"  [applies to: {paths}]"
        items.append((rel, re.sub(r"\s+", " ", desc).strip()))
    return items


def collect_json_dir(folder, label_key=None):
    items = []
    for p in sorted(glob.glob(os.path.join(folder, "*.json"))):
        name = os.path.basename(p)
        desc = ""
        try:
            data = json.load(open(p, encoding="utf-8"))
            desc = data.get("description") or data.get("title") or ""
            if not desc and isinstance(data, dict):
                desc = f"{len(data)} top-level keys: " + ", ".join(list(data)[:6])
        except Exception:
            desc = "(json config)"
        items.append((name, re.sub(r"\s+", " ", str(desc)).strip()[:140]))
    return items


def collect_subdir_readmes(folder):
    items = []
    for d in sorted(os.listdir(folder)):
        sub = os.path.join(folder, d)
        if not os.path.isdir(sub):
            continue
        rd = os.path.join(sub, "README.md")
        desc = oneline(rd) if os.path.exists(rd) else "(module)"
        items.append((d, desc))
    return items


def main():
    parts = ["# ECC TASK CATALOG",
             "\n_Permanent-memory menu of every ECC capability. The dispatcher "
             "reads this to map a task → persona + skills + rules + commands._\n"]

    parts.append(section("Personas (agents)",
                         collect_md_dir(os.path.join(ECC, "agents"))))
    parts.append(section("Skills", collect_skills()))
    parts.append(section("Commands",
                         collect_md_dir(os.path.join(ECC, "commands"))))
    parts.append(section("Rules", collect_rules()))

    # hooks
    hooks = []
    hjson = os.path.join(ECC, "hooks", "hooks.json")
    if os.path.exists(hjson):
        hooks.append(("hooks.json", oneline_json(hjson)))
    for d in sorted(os.listdir(os.path.join(ECC, "hooks"))):
        sub = os.path.join(ECC, "hooks", d)
        if os.path.isdir(sub):
            rd = os.path.join(sub, "README.md")
            hooks.append((d, oneline(rd) if os.path.exists(rd) else "(hook module)"))
    parts.append(section("Hooks", hooks))

    parts.append(section("Contexts",
                         collect_md_dir(os.path.join(ECC, "contexts"))))
    parts.append(section("Schemas",
                         collect_json_dir(os.path.join(ECC, "schemas"))))
    parts.append(section("Integrations",
                         collect_subdir_readmes(os.path.join(ECC, "integrations"))))

    shims = os.path.join(ECC, "legacy-command-shims", "commands")
    if os.path.isdir(shims):
        parts.append(section("Legacy command shims", collect_md_dir(shims)))

    parts.append(section("Plugins",
                         [("README", oneline(os.path.join(ECC, "plugins", "README.md")))]))
    parts.append(section("MCP configs",
                         collect_json_dir(os.path.join(ECC, "mcp-configs"))))
    parts.append(section("Config",
                         collect_json_dir(os.path.join(ECC, "config"))))

    open(OUT, "w", encoding="utf-8").write("\n".join(parts))
    # count
    total = sum(1 for ln in open(OUT, encoding="utf-8") if ln.startswith("- **"))
    print(f"wrote {OUT}  ({total} catalog entries)")


def oneline_json(p):
    try:
        data = json.load(open(p, encoding="utf-8"))
        if isinstance(data, dict) and "hooks" in data:
            return f"hook registry: {len(data.get('hooks') or [])} hook(s) wired to lifecycle events"
        return "hook configuration"
    except Exception:
        return "hook configuration"


if __name__ == "__main__":
    main()
