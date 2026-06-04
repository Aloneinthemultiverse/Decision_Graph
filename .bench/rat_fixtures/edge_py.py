"""Edge cases for rationale extraction."""

# WHY: top-level rationale (no enclosing symbol)
# It should produce a rationale with symbol_id=NULL

import os

class Outer:
    # NOTE: rationale inside class body (no method yet)
    x = 1

    def method_a(self):
        # HACK: classic single-line rationale
        return self.x

    class Inner:
        # SAFETY: nested-class rationale — should attach to Inner, not Outer
        def deep_method(self):
            # BUG: rationale inside the innermost method
            # FIXME: a follow-up note on same scope
            pass


def free_function():
    """Docstring is not a rationale."""
    # PERF: ascii rationale
    # WARNING: ünïcödë characters in rationale — μ, λ, π, 中文, مرحبا
    return 42


# DEPRECATED: rationale right at file footer
