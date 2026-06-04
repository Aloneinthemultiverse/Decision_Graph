"""Phase 2 — CAG layer: tiered blueprint slicer (DG-CAG).

The blueprint markdown (built at ingest by codebase.py) is the CAG cache. It is
big (~130-240KB / ~30-60k tokens). We do NOT preload all of it. Instead we tier:

  ALWAYS    — the small repo overview (Mission / Key Concepts / Stack), every session
  ON-DEMAND — the per-file `## File:` sections, but only for the folders the task touches

`get_context_pack(blueprint_path, task_files, token_budget)` returns the right
slice as one markdown string + metadata, fast (pure text, no LLM, no graph load).

Blueprint structure (from codebase._build_repo_blueprint):
  # Project: owner/repo
  ## Mission ...
  ## Key Concepts ...
  ## Stack ...                 <- overview (everything before the first '## File:')
  # Files in owner/repo
  ## File: <path>              <- one section per file, in original order
  ...
  # Recent activity ...        <- tail (top-level '#', after the file sections)
  # Recent pull requests ...
"""
from __future__ import annotations

import os
import re
from typing import Optional

# rough chars-per-token for budgeting (markdown/english ~4)
_CHARS_PER_TOKEN = 4
_DEFAULT_TOKEN_BUDGET = 6000

_FILE_HDR = re.compile(r"^## File:\s*(.+?)\s*$", re.MULTILINE)
_TOP_HDR = re.compile(r"^# \S", re.MULTILINE)


def _split_blueprint(text: str) -> tuple[str, dict[str, str], str]:
    """Split blueprint into (overview, {path: section}, tail).

    overview = everything before the first '## File:'.
    each file section runs from its '## File:' line up to the next '## File:'
    or the next top-level '# ' header (whichever comes first).
    tail = trailing top-level sections after the last file (recent activity/PRs).
    """
    first = _FILE_HDR.search(text)
    if not first:
        return text, {}, ""

    overview = text[: first.start()].rstrip() + "\n"
    body = text[first.start():]

    # positions of every '## File:' header
    hdrs = list(_FILE_HDR.finditer(body))
    sections: dict[str, str] = {}
    tail = ""

    for i, m in enumerate(hdrs):
        path = m.group(1).strip()
        start = m.start()
        end = hdrs[i + 1].start() if i + 1 < len(hdrs) else len(body)
        chunk = body[start:end]

        # a top-level '# ' header (recent activity / PRs) may appear inside the
        # last chunk — cut the section there and keep the rest as tail.
        if i + 1 == len(hdrs):
            top = _TOP_HDR.search(chunk, 1)  # skip pos 0 (the '## File' line)
            if top:
                tail = chunk[top.start():].rstrip() + "\n"
                chunk = chunk[: top.start()]
        sections[path] = chunk.rstrip() + "\n"

    return overview, sections, tail


def _approx_tokens(s: str) -> int:
    return max(1, len(s) // _CHARS_PER_TOKEN)


def _folder_of(path: str) -> str:
    return os.path.dirname(path.replace("\\", "/")) or "."


def get_context_pack(
    blueprint_path: str,
    task_files: Optional[list[str]] = None,
    token_budget: int = _DEFAULT_TOKEN_BUDGET,
    include_folder_siblings: bool = True,
) -> dict:
    """Return a tiered slice of the blueprint for the files a task touches.

    Args:
        blueprint_path: path to the repo's blueprint .md (the CAG cache).
        task_files: files the task will touch. If empty -> overview only.
        token_budget: approximate token cap for the returned pack.
        include_folder_siblings: also include other files in the same folder(s)
            as the task files (the "area" the task touches), budget permitting.

    Returns dict:
        markdown        — the assembled context pack (overview + selected sections)
        included_files  — file paths whose sections were included
        skipped_files   — task files not found in the blueprint
        approx_tokens   — rough token estimate of `markdown`
        total_files     — number of file sections in the blueprint
        truncated       — True if budget forced sections to be dropped
    """
    if not os.path.exists(blueprint_path):
        return {"error": f"blueprint not found: {blueprint_path}"}

    with open(blueprint_path, "r", encoding="utf-8") as f:
        text = f.read()

    overview, sections, _tail = _split_blueprint(text)
    task_files = [t.replace("\\", "/") for t in (task_files or [])]

    parts = [overview]
    budget = max(token_budget, _approx_tokens(overview))
    used = _approx_tokens(overview)
    included: list[str] = []
    skipped: list[str] = []
    truncated = False

    # rank candidate sections: exact task files first, then folder siblings.
    exact = [p for p in task_files if p in sections]
    skipped = [p for p in task_files if p not in sections]

    siblings: list[str] = []
    if include_folder_siblings and exact:
        task_folders = {_folder_of(p) for p in exact}
        for p in sections:
            if p in exact:
                continue
            if _folder_of(p) in task_folders:
                siblings.append(p)

    ordered = exact + siblings
    for p in ordered:
        sec = sections[p]
        cost = _approx_tokens(sec)
        if used + cost > budget:
            truncated = True
            continue
        parts.append(sec)
        used += cost
        included.append(p)

    return {
        "markdown": "\n".join(parts).rstrip() + "\n",
        "included_files": included,
        "skipped_files": skipped,
        "approx_tokens": used,
        "total_files": len(sections),
        "truncated": truncated,
    }
