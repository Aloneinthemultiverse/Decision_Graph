# Side-by-side: agent answering "Add mark_dirty() to AppContext"

## Without our graph (vanilla coding agent)

> The agent reads `src/flask/ctx.py` cold.

```python
# Suggested change
class AppContext:
    def __init__(self, app):
        self.app = app
        # ... existing ...
        self._dirty = False

    def mark_dirty(self):
        """Mark this context as modified."""
        self._dirty = True
```

**Caveats it offers (generic, not codebase-specific):**
- "Consider thread safety"
- "You might want a corresponding `is_dirty()` getter"
- "Add a docstring and unit tests"

**What it misses entirely:**
- doesn't know who instantiates AppContext
- doesn't know AppContext is a god-node
- doesn't know contextvars is the active design choice
- doesn't know prior PR #5341 fixed teardown ordering bugs
- doesn't warn about coordination with downstream teams

---

## With our graph (same agent, with MCP tools)

> Calls `causal_radius("src/flask/ctx.py")` first.

```python
# Suggested change
class AppContext:
    def __init__(self, app):
        self.app = app
        # ... existing ...
        # Mirror the contextvars pattern already used in this file
        # (ADR-2026-01: context propagation via contextvars, not thread-local).
        self._dirty_token: contextvars.Token | None = None
        self._dirty = False

    def mark_dirty(self) -> None:
        """Signal this context has been modified.

        Important: this must be called BEFORE pop() because PR #5341 fixed
        a bug where teardown_appcontext fired twice when current_app was
        replaced mid-request. Setting _dirty after pop() would re-trigger
        that path.
        """
        self._dirty = True

    def is_dirty(self) -> bool:
        return self._dirty
```

**Codebase-specific warnings the with-graph agent gives:**

1. **Risk: MEDIUM (41.1/100)** — touching `ctx.py` puts you in the merge-last queue.
2. **`AppContext` is a god-node** with 48 callers via `_AppCtxGlobals.get`. Schedule a code review with whoever owns the App-level globals.
3. **Blast radius: 39 files** including `app.py`, `templating.py`, and 11 test files. CI on the full test suite, not just `test_appctx.py`.
4. **ADR alignment:** the existing design (ADR-2026-01) uses contextvars for context propagation. Your `_dirty` flag should respect the same pattern or document the deviation.
5. **PR #5341 precedent:** there was a bug where teardown fired twice on `current_app` replacement. Your `mark_dirty()` must not run AFTER `pop()` or you'll regress this.
6. **Commit a29f88ce note:** there's an undocumented invariant — headers must be set before streaming starts. If your new method ever runs during a streaming response, document the interaction.
7. **Test files to update (from blast radius):** `tests/test_appctx.py`, `tests/test_basic.py`, `tests/test_reqctx.py`, `tests/test_session_interface.py`, plus 7 more.

**What it specifically refused to do without input:**
- skipped adding `mark_dirty()` to RequestContext (its god-node status is different and the dirty-flag semantics may not transfer)
- did NOT add to the public Flask `__init__.py` exports — `AppContext` is exported but `mark_dirty` is internal until reviewed

---

## The honest delta

| Dimension | Vanilla | With-graph |
|---|---|---|
| Code generated | yes | yes |
| Aware of god-nodes | no | yes |
| Cites prior ADRs | no | yes |
| Cites prior PRs/bugs | no | yes |
| Predicts test files to update | no | yes (11 test files named) |
| Risk score | no | 41.1 MEDIUM |
| Merge-order hint | no | "merge-last" |
| Time to produce answer | ~1s | ~2s (one extra MCP call) |

**The point:** the code itself is similar quality. The *explanation, risk awareness, and team-coordination guidance* is the differentiator. That's what a senior engineer would add on top of any junior engineer's diff — and that's what our graph gives the agent for free.
