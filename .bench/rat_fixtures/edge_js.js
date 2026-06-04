// WHY: top-level JS comment

class A {
  // NOTE: inside class
  foo() {
    // HACK: single-line in method
    /* TODO: block comment in method */
    return 1;
  }
}

// FIXME: block-style follow-up
/*
 * SAFETY: multi-line block — should still get tagged
 * with the SAFETY tag pulled from the first line
 */
function bar() { return 2; }

// WARNING: ünïcödë in JS — λ μ
