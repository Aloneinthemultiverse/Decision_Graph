# Architecture Decision: SQLite for code graph

## Context
We needed fast structural queries over 1M+ symbols.

## Decision
Use SQLite with WAL + careful indexing.

## Consequences
- Sub-millisecond queries
- No external DB dependency
