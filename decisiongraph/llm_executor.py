"""Phase 5 — the REAL executor: the OS's agent brain that writes code.

This replaces the scripted stub used in the offline demo. It is the callable the
Kernel invokes for JOB 4 (RUN). Given the kernel's `contract` — which already
carries the DG context slice (the blueprint the kernel LOADed for this task) —
it asks the configured LLM to actually write the file content, then returns the
edits/decisions in the shape the kernel logs + writes back to DG.

DG is in the loop on BOTH sides:
  * IN   — contract["context_markdown"] is the DG-CAG slice; it goes into the prompt.
  * OUT  — the kernel write-back stores the decision into DG memory and feeds the
           edits into the code graph.

The model talks the Anthropic `messages` API (our local gateway speaks it). We
reuse DG's own client so the same gateway/model config drives everything.
"""
from __future__ import annotations

import os
import re
import time


def messages_with_retry(client, *, retries: int = 20, backoff: float = 8.0,
                        verbose: bool = True, timeout: float = 150.0, **kwargs):
    """Call client.messages.create, surviving a FLAPPING gateway: retry on
    connection/timeout errors with capped backoff so a brief outage (seconds)
    doesn't kill a long multi-call build. A per-request `timeout` turns a HUNG
    gateway into a fast retryable error instead of a ~10-min stall.
    Re-raises only after exhausting retries."""
    last = None
    for attempt in range(retries + 1):
        try:
            return client.with_options(timeout=timeout).messages.create(**kwargs)
        except Exception as e:
            name = type(e).__name__
            transient = ("Connection" in name or "Timeout" in name
                         or "APIStatus" in name or "Overloaded" in name
                         or "InternalServer" in name)
            last = e
            if not transient or attempt == retries:
                raise
            wait = min(backoff * (attempt + 1), 40.0)
            if verbose:
                print(f"      [retry] {name} on LLM call — waiting {wait:.0f}s "
                      f"(attempt {attempt+1}/{retries})")
            time.sleep(wait)
    raise last

_FILE_RE = re.compile(r"===FILE:\s*(?P<path>[^\n=]+?)\s*===\n(?P<body>.*?)\n===END===",
                      re.DOTALL)

_SYS = (
    "You are a senior engineer working as a coding agent inside an automated build "
    "system. You write COMPLETE, CORRECT, production-quality files — never stubs, "
    "never TODOs, never placeholders. Output ONLY the files, each wrapped exactly as:\n"
    "===FILE: <relative/path>===\n<full file content>\n===END===\n"
    "No prose, no markdown fences, no commentary outside the markers."
)


def _extract_text(msg) -> str:
    """Pull the assistant text out of an Anthropic-style response (skip thinking)."""
    parts = []
    for block in getattr(msg, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def make_llm_executor(client, model: str, site_dir: str, *,
                      max_tokens: int = 8000, verbose: bool = True,
                      extra_context_fn=None, advice_context_fn=None):
    """Return an executor(contract) -> result that REALLY writes the files.

    extra_context_fn(contract) -> str : optional. Lets the caller inject live
    project knowledge (e.g. exact symbol signatures from the code graph) so the
    agent imports real names instead of hallucinating modules.
    """

    def executor(contract: dict) -> dict:
        task = contract["task"]
        agent = contract.get("agent", "engineer")
        targets = contract.get("task_files") or []
        ctx = (contract.get("context_markdown") or "").strip()

        extra = ""
        if extra_context_fn is not None:
            try:
                extra = (extra_context_fn(contract) or "").strip()
            except Exception:
                extra = ""

        advice = ""
        if advice_context_fn is not None:
            try:
                advice = (advice_context_fn(contract) or "").strip()
            except Exception:
                advice = ""

        ctx_block = (f"\n\n# Project context (from DecisionGraph memory)\n{ctx}\n"
                     if ctx else "\n\n(No prior project context yet — this is an early file.)\n")
        target_block = "\n".join(f"  - {t}" for t in targets) or "  (decide sensible paths yourself)"

        # Put the EXACT existing signatures up front and make them binding — a
        # weak model ignores a symbols dump buried at the bottom, so we lead with
        # it and spell out the contract: call these names with these exact args.
        contract_block = ""
        if extra:
            contract_block = (
                "\n# BINDING CONTRACT — existing code already written (from the code graph)\n"
                "These functions/classes ALREADY EXIST. You MUST call them with the EXACT\n"
                "names and argument signatures shown, and you MUST treat their RETURN VALUE\n"
                "exactly as the `# returns ->` hint shows (e.g. if it returns an id string,\n"
                "do NOT treat the result as an object). Do NOT change argument shapes, do NOT\n"
                "pass a dict where positional args are shown, do NOT rename keys, and do NOT\n"
                "invent modules or functions not listed here:\n"
                f"{extra}\n")

        advice_block = ""
        if advice:
            advice_block = (
                "\n# Recommended ECC skills & rules (GUIDANCE ONLY — apply this "
                "know-how and follow these standards; these are NOT functions to "
                "call or import):\n"
                f"{advice}\n")

        persona = (contract.get("agent_persona") or "").strip()
        persona_block = (f"# YOU ARE THE `{agent}` AGENT — adopt this role fully:\n"
                         f"{persona}\n\n" if persona
                         else f"You are acting as the `{agent}` agent.\n")

        # shared preamble used for EVERY single-file call (one file per call)
        preamble = (
            f"{persona_block}"
            f"TASK: {task}\n"
            f"{contract_block}\n"
            f"{advice_block}\n"
            f"{ctx_block}\n"
            "Requirements:\n"
            "- Reuse the EXACT names, argument signatures and exported shapes from the "
            "binding contract above so imports and data shapes line up across files.\n"
            "- Make the code real and consistent — no stubs, no TODOs, no fragments.\n"
            "- If a file is a web page, it must actually work in a browser.\n"
        )

        edits, tool_calls = [], []
        written: set[str] = set()
        err = None

        def _clean(body):
            body = re.sub(r"(?m)^\s*===\s*(FILE|END)\b.*$", "", body)
            body = re.sub(r"(?m)^\s*```[a-zA-Z0-9]*\s*$", "", body)
            return body.strip("\n")

        def _looks_broken(path, body):
            """Cheap fragment/truncation guard — reject obviously corrupt files
            (e.g. a body that starts mid-expression, or wildly unbalanced braces)."""
            b = body.strip()
            if not b:
                return "empty"
            # a real source file shouldn't start with a closing/continuation token
            if re.match(r"^[)\]}/,;:.]|^/g|^\*/|^=>", b):
                return "starts mid-expression (fragment)"
            if path.endswith((".ts", ".tsx", ".js", ".jsx", ".json", ".css")):
                if b.count("{") != b.count("}"):
                    return "unbalanced braces (likely truncated)"
                if b.count("[") != b.count("]"):
                    return "unbalanced brackets (likely truncated)"
                if abs(b.count("(") - b.count(")")) > 1:
                    return "unbalanced parens (likely truncated)"
                # truncated mid-statement: ends on an opening/continuation token
                if b.rstrip()[-1:] in {",", "(", "[", "{", ":", "=", "+", "&", "|", '"', "'"}:
                    return "ends mid-statement (truncated)"
            return None

        def _persist(path, body):
            body = _clean(body)
            path = path.strip().lstrip("/").replace("\\", "/")
            abs_path = os.path.join(site_dir, path)
            os.makedirs(os.path.dirname(abs_path) or site_dir, exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(body if body.endswith("\n") else body + "\n")
            edits.append({"path": path, "summary": f"wrote {path}", "text": body})
            tool_calls.append({"tool": "Write", "path": path, "bytes": len(body)})
            written.add(path)

        def _write_one(want):
            """ONE file, ONE call (with up to 2 retries + a validation gate). This
            removes the multi-file split that corrupted files like route.ts."""
            single = (preamble +
                      f"\nWrite EXACTLY ONE file now: `{want}` — the COMPLETE file, "
                      f"nothing else. Output it as:\n"
                      f"===FILE: {want}===\n<full file content>\n===END===")
            for attempt in range(3):
                try:
                    msg = messages_with_retry(client, verbose=verbose,
                        model=model, max_tokens=max_tokens, system=_SYS,
                        messages=[{"role": "user", "content": single}])
                    text = _extract_text(msg)
                except Exception as e:
                    return f"{type(e).__name__}: {e}"
                m = _FILE_RE.search(text)
                body = _clean(m.group("body") if m else text)
                bad = _looks_broken(want, body)
                if bad:
                    if verbose:
                        print(f"      [executor] rejected {want} (attempt {attempt+1}): {bad}")
                    single += (f"\n\nYour previous output for `{want}` was invalid "
                               f"({bad}). Output the COMPLETE, valid file again.")
                    continue
                _persist(want, body)
                return None
            return f"could not get a valid `{want}` after retries"

        targets_norm = [t.replace("\\", "/").lstrip("/") for t in targets] or []
        try:
            if targets_norm:
                for want in targets_norm:          # ONE FILE PER CALL
                    e = _write_one(want)
                    if e and verbose:
                        print(f"      [executor] {want}: {e}")
            else:
                # no explicit targets → single freeform call, persist any files found
                msg = messages_with_retry(client, verbose=verbose,
                    model=model, max_tokens=max_tokens, system=_SYS,
                    messages=[{"role": "user", "content": preamble +
                               "\nOutput each file with ===FILE: path=== / ===END==="}])
                for m in _FILE_RE.finditer(_extract_text(msg)):
                    _persist(m.group("path"), m.group("body"))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"

        if verbose:
            if err:
                print(f"      [executor] LLM error: {err}")
            else:
                print(f"      [executor] {agent} wrote {len(edits)} file(s) "
                      f"via {model}: {', '.join(e['path'] for e in edits) or '(none)'}")

        result = {
            "tool_calls": tool_calls,
            "edits": edits,
            "decisions": [{
                "question": f"How should we implement: {task[:70]}?",
                "answer": f"{agent} produced {', '.join(e['path'] for e in edits) or 'nothing'}.",
                "reasoning": f"Generated by {model} using the DG context slice "
                             f"({len(ctx)} chars of prior project memory).",
            }],
            "notes": [f"agent={agent}"] + ([f"error={err}"] if err else []),
        }
        return result

    return executor
