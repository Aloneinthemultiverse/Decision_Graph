"""Fast structural-only rebuild — no LLM, no semantic blueprint.

Designed to run as a git post-commit hook so the code graph stays current
without any API cost. Typical runtime on a 1000-file repo: 2-5 seconds.

Usage:
    python -m decisiongraph.code_graph_cli rebuild [--repo-path PATH] [--db PATH] [--repo-name NAME]
    python -m decisiongraph.code_graph_cli install-hook [--repo-path PATH]

Install:
    cd <your-repo>
    python -m decisiongraph.code_graph_cli install-hook
    # → writes .git/hooks/post-commit that calls `rebuild` on every commit
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

from . import code_graph as cg


def _default_repo_name(repo_path: Path) -> str:
    """Best-effort repo name from git remote, else folder name."""
    try:
        import subprocess
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_path), capture_output=True, text=True
        ).stdout.strip()
        if out:
            # parse owner/repo from url
            tail = out.rstrip("/").rstrip(".git")
            parts = tail.replace(":", "/").split("/")
            if len(parts) >= 2:
                return "/".join(parts[-2:])
    except Exception:
        pass
    return repo_path.name


def cmd_rebuild(args) -> int:
    repo_path = Path(args.repo_path or os.getcwd()).resolve()
    db = args.db or str(repo_path / ".dg_code_graph.db")
    repo_name = args.repo_name or _default_repo_name(repo_path)
    print(f"[code-graph] rebuilding {repo_name} ({repo_path}) -> {db}")
    s = cg.build_from_repo(db, str(repo_path), repo_name)
    print(f"[code-graph] OK: files={s['files']} symbols={s['symbols']} "
          f"calls={s['calls']} imports={s['imports']} "
          f"inherits={s['inherits']} rationales={s['rationales']} "
          f"({s['elapsed_s']}s)")
    return 0


_HOOK_MARKER = "# DG-AUTO-INSTALLED-HOOK"


def cmd_install_hook(args) -> int:
    repo_path = Path(args.repo_path or os.getcwd()).resolve()
    hooks_dir = repo_path / ".git" / "hooks"
    if not hooks_dir.is_dir():
        print(f"[code-graph] {hooks_dir} not found — is this a git repo?",
              file=sys.stderr)
        return 1
    hook = hooks_dir / "post-commit"
    # Existing-hook detection: if there's already a post-commit hook we
    # didn't install (no marker), preserve it by appending instead.
    existing = ""
    if hook.exists():
        existing = hook.read_text(encoding="utf-8", errors="replace")
        if _HOOK_MARKER in existing:
            print(f"[code-graph] DG hook already installed — refreshing.")
            existing = ""    # rewrite our own
        else:
            print(f"[code-graph] WARNING: existing post-commit hook detected. "
                  f"Appending DG section; original behaviour preserved.")
    repo_name = args.repo_name or _default_repo_name(repo_path)
    db = args.db or str(repo_path / ".dg_code_graph.db")
    python_exe = sys.executable.replace("\\", "/")
    # Bake PYTHONPATH into the hook so it finds the decisiongraph package
    # whether or not the user installed it via pip. We inherit the runtime
    # path that's in effect at install time.
    pkg_root = str(Path(__file__).resolve().parent.parent).replace("\\", "/")
    # Synchronous run — rebuild is <1s on typical repos, no need to background.
    # Backgrounding via `&` is unreliable on Windows Git-Bash anyway.
    dg_block = (
        f"\n{_HOOK_MARKER}-BEGIN\n"
        f'# Auto-installed by DecisionGraph code_graph_cli\n'
        f'# Rebuilds the structural code graph after every commit (no LLM).\n'
        f'export PYTHONPATH="{pkg_root}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        f'"{python_exe}" -m decisiongraph.code_graph_cli rebuild \\\n'
        f'    --repo-path "{repo_path}" \\\n'
        f'    --repo-name "{repo_name}" \\\n'
        f'    --db "{db}" \\\n'
        f'    >/dev/null 2>&1 || true\n'
        f"{_HOOK_MARKER}-END\n"
    )
    if existing:
        body = existing.rstrip("\n") + dg_block
    else:
        body = "#!/bin/sh\n" + dg_block
    hook.write_text(body, encoding="utf-8")
    try:
        os.chmod(hook, 0o755)
    except Exception:
        pass
    print(f"[code-graph] installed -> {hook}")
    print(f"[code-graph] repo_name={repo_name}  db={db}")
    print(f"[code-graph] every `git commit` will now refresh the graph "
          f"in the background.")
    return 0


def cmd_uninstall_hook(args) -> int:
    """Remove the DG block from the post-commit hook. If the hook becomes
    empty after removal (just shebang), delete the file entirely."""
    import re
    repo_path = Path(args.repo_path or os.getcwd()).resolve()
    hook = repo_path / ".git" / "hooks" / "post-commit"
    if not hook.exists():
        print(f"[code-graph] no post-commit hook at {hook}")
        return 0
    content = hook.read_text(encoding="utf-8", errors="replace")
    if _HOOK_MARKER not in content:
        print(f"[code-graph] post-commit hook exists but wasn't installed by us; "
              f"leaving it alone.")
        return 0
    new = re.sub(
        rf"\n?{re.escape(_HOOK_MARKER)}-BEGIN.*?{re.escape(_HOOK_MARKER)}-END\n",
        "", content, flags=re.DOTALL)
    # if all that's left is the shebang (or nothing), remove the file
    if new.strip() in ("", "#!/bin/sh"):
        hook.unlink()
        print(f"[code-graph] uninstalled (hook file removed)")
    else:
        hook.write_text(new, encoding="utf-8")
        print(f"[code-graph] uninstalled (preserved {len(new)} bytes of "
              f"pre-existing hook content)")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="code_graph_cli",
        description="Structural-only code graph (no LLM, no API cost)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("rebuild",
        help="Re-ingest a repo into the SQLite structural graph")
    sp.add_argument("--repo-path", default=None)
    sp.add_argument("--repo-name", default=None)
    sp.add_argument("--db", default=None)
    sp.set_defaults(fn=cmd_rebuild)

    sp2 = sub.add_parser("install-hook",
        help="Write a git post-commit hook that auto-rebuilds the graph")
    sp2.add_argument("--repo-path", default=None)
    sp2.add_argument("--repo-name", default=None)
    sp2.add_argument("--db", default=None)
    sp2.set_defaults(fn=cmd_install_hook)

    sp3 = sub.add_parser("uninstall-hook",
        help="Remove the DG post-commit hook (preserves any pre-existing hook)")
    sp3.add_argument("--repo-path", default=None)
    sp3.set_defaults(fn=cmd_uninstall_hook)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
