---
name: check_duplicate
metadata:
  version: 11.20.0
  tier: read-only
description: Check a proposed entity or concept against Vector Lake before any creation or merge decision.
---

# Duplicate check

Call `check_duplicate_entity` from `vector-lake-mcp` with a precise `candidate_title`, supported `candidate_type`, and a short factual `candidate_summary` when available.

- Treat similarity results as candidates, not automatic identity decisions.
- Report matching titles, identifiers or paths, scores, and material differences returned by the tool.
- Do not create, merge, rename, or update an entity in this skill. Any write requires a separately authorized workflow.
- If the evidence is ambiguous, state the ambiguity and ask the user to choose between reuse, merge review, or separate creation.
