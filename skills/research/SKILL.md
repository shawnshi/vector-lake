---
name: research
metadata:
  version: 11.20.0
  tier: action-allowed
description: Research current Vector Lake evidence gaps and persist sourced findings only to the effective runtime's research intake.
---

# Evidence-gap research

Use `trigger_autonomous_research` from `vector-lake-mcp` with `dry_run=True` to preview bounded research directives.

1. Determine the effective runtime MEMORY root from the current runtime, using the `MEMORY` path reported by `doctor_vector_lake`; fail closed if it is missing or ambiguous.
2. Define the only research intake as `<effective MEMORY>/raw/research`. Never substitute a fixed `.gemini`, user-profile, checkout, or plugin path.
3. Present the directives and obtain explicit authorization before calling `trigger_autonomous_research(dry_run=False)`, performing external research, or writing a research source.
4. Write only sourced findings authorized by the user, with URLs, retrieval dates, and a clear fact/inference boundary.
5. If ingestion is requested, call `sync_vector_lake` and report its scan/enqueue receipt.

`sync_vector_lake` only scans and enqueues bounded ingest jobs. It does not generate or finalize Wiki pages and is not proof that ingestion completed.
