"""The DISPATCHER — the OS's brain for routing.

It is NOT a heuristic. It is an LLM call driven by the ECC Universal Task
Dispatcher master system prompt (ecc/DISPATCHER_PROMPT.md). Given ONE high-level
task it:
  * decomposes the task per the prompt's decomposition ladder,
  * maps each sub-task to a catalog SECTION,
  * names the agents / skills / commands / rules for each sub-task,
  * orders them by the dependency chain.

Claude's job is only to wire + run + evaluate this — the dispatcher (this prompt)
does the choosing. Output is parsed into structured sub-tasks the kernel then
executes (builder = persona + its skills).
"""
from __future__ import annotations
import os, re, json

_OUTPUT_CONTRACT = """

━━━ MACHINE OUTPUT (required) ━━━
After your normal dispatch reasoning, output a single fenced ```json block with
this exact shape (no prose after it):

```json
{
  "task_type": "<primary category, e.g. A-03 Full Stack>",
  "cross_cutting": ["skill-or-agent", ...],
  "subtasks": [
    {
      "id": 1,
      "title": "short title",
      "step": "PLAN|DATA|BACKEND|FRONTEND|INTEGRATE|SECURE|TEST|DEPLOY|DOCUMENT",
      "section": "A-18",
      "description": "what to build, concretely",
      "files": ["relative/path.ext", ...],
      "agents": ["agent-name", ...],
      "skills": ["skill-name", ...],
      "commands": ["/cmd", ...],
      "rules": ["rule/name", ...]
    }
  ]
}
```
Order subtasks by the dependency chain (PLAN→DATA→BACKEND→FRONTEND→INTEGRATE→
SECURE→TEST→DEPLOY→DOCUMENT). Pick agents/skills/rules ONLY from the sections you
matched.

MANDATORY PROJECT STRUCTURE (a web app MUST use this EXACT Next.js App Router
layout so it boots — do NOT invent other roots):
  - src/app/page.tsx              ← REQUIRED homepage that imports & renders the sections
  - src/app/layout.tsx            ← root layout
  - src/app/api/<name>/route.ts   ← API routes
  - src/components/<Name>.tsx      ← UI components (flat, not deeply nested)
  - src/lib/<name>.ts              ← utilities/validation
  - src/data/<name>.ts             ← static data
  - prisma/schema.prisma           ← schema (only if a DB is truly needed)
  - src/__tests__/<name>.test.tsx  ← tests (use .tsx if they contain JSX)
Rules: EVERY source file path MUST start with `src/` (except prisma/ and root
config). One of the FRONTEND subtasks MUST output `src/app/page.tsx` that imports
each component and renders the full landing page. Import components via the `@/`
alias (e.g. `import Hero from '@/components/Hero'`).
"""

_JSON_RE = re.compile(r"```json\s*(?P<body>\{.*?\})\s*```", re.DOTALL)


def load_dispatcher_prompt(path: str) -> str:
    return open(path, encoding="utf-8").read()


def _extract_text(msg) -> str:
    parts = []
    for block in getattr(msg, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def is_software_task(plan: dict) -> bool:
    """SECURE/TEST/scaffold/boot-gate only make sense for code. Detect whether this
    is a software build (sections A/B/C/M) vs a content/business/ops task (E/F/G/H/
    I/K) so we don't inject pytest into a marketing campaign."""
    tt = (plan.get("task_type") or "").strip().upper()
    code_sections = ("A-", "B-", "C-", "M-")
    if tt.startswith(code_sections):
        return True
    # else look at the subtasks' sections / file extensions
    for s in plan.get("subtasks") or []:
        if (s.get("section") or "").upper().startswith(code_sections):
            return True
        if any(str(f).endswith((".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs",
                                ".java", ".kt", ".swift", ".cpp"))
               for f in (s.get("files") or [])):
            return True
    return False


def _enforce_mandatory(plan: dict) -> dict:
    """The master prompt says SECURE and TEST must NEVER be skipped — FOR CODE.
    Inject them only for software tasks (not marketing/research/ops)."""
    if not is_software_task(plan):
        return plan
    subs = plan.get("subtasks") or []

    def has(step, section):
        return any((s.get("step") or "").upper() == step
                   or (s.get("section") or "") == section for s in subs)

    # candidate source files already planned (to point SECURE at real API/forms)
    all_files = [f for s in subs for f in (s.get("files") or [])]
    api_files = [f for f in all_files if "/api/" in f or "route" in f.lower()
                 or "form" in f.lower()]
    next_id = (max((s.get("id", 0) for s in subs), default=0)) + 1
    injected = []

    if not has("SECURE", "A-14"):
        injected.append({
            "id": next_id, "title": "Input validation & security hardening",
            "step": "SECURE", "section": "A-14",
            "description": ("Add input validation and security hardening (sanitize "
                            "inputs, validate request bodies, prevent XSS/injection) "
                            "to the API routes and forms."),
            "files": api_files[:2] or ["src/lib/validation.ts"],
            "agents": ["security-reviewer"], "skills": ["security-review"],
            "commands": [], "rules": ["common/security"]})
        next_id += 1

    if not has("TEST", "A-12"):
        injected.append({
            "id": next_id, "title": "Tests for core logic",
            "step": "TEST", "section": "A-12",
            "description": "Write tests covering the core components and API logic.",
            "files": ["src/__tests__/app.test.tsx"],
            "agents": ["pr-test-analyzer"], "skills": ["tdd-workflow"],
            "commands": [], "rules": ["common/testing"]})

    if injected:
        # insert SECURE/TEST before any DOCUMENT step, else append
        doc_i = next((i for i, s in enumerate(subs)
                      if (s.get("step") or "").upper() == "DOCUMENT"), len(subs))
        plan["subtasks"] = subs[:doc_i] + injected + subs[doc_i:]
        plan["_injected_steps"] = [s["step"] for s in injected]
    return plan


def dispatch_task(client, model: str, prompt_path: str, task: str,
                  *, max_tokens: int = 8000, verbose: bool = True) -> dict:
    """Run the dispatcher LLM over a high-level task. Returns the parsed plan:
    {task_type, cross_cutting[], subtasks[...]} plus 'raw' (the model's text)."""
    from .llm_executor import messages_with_retry
    system = load_dispatcher_prompt(prompt_path)
    user = (f"TASK: {task}\n\nDecompose and dispatch this task using your protocol."
            + _OUTPUT_CONTRACT)
    msg = messages_with_retry(client, verbose=verbose,
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = _extract_text(msg)
    plan = {"task_type": "", "cross_cutting": [], "subtasks": [], "raw": text}
    m = _JSON_RE.search(text)
    if m:
        try:
            data = json.loads(m.group("body"))
            plan.update({k: data.get(k, plan[k])
                         for k in ("task_type", "cross_cutting", "subtasks")})
        except Exception as e:
            plan["parse_error"] = f"{type(e).__name__}: {e}"
    else:
        plan["parse_error"] = "no ```json block found in dispatcher output"
    # NOTE: SECURE/TEST/SCAFFOLD are now MANDATED BY THE SYSTEM PROMPT itself
    # (see DISPATCHER_PROMPT.md → "MANDATORY BUILD RULES"). The dispatcher LLM
    # emits them — we no longer inject them from Python. Kept as an opt-in net.
    if os.environ.get("DG_ENFORCE_PY") == "1":
        plan = _enforce_mandatory(plan)
    if verbose:
        print(f"   [dispatcher] task_type={plan['task_type']} "
              f"subtasks={len(plan['subtasks'])} "
              f"cross_cutting={plan['cross_cutting']}")
        if plan.get("_injected_steps"):
            print(f"   [dispatcher] injected mandatory steps: {plan['_injected_steps']}")
        if plan.get("parse_error"):
            print(f"   [dispatcher] WARN: {plan['parse_error']}")
    return plan
