---
name: merge
metadata:
  version: 11.20.0
  tier: action-allowed
description: Analyze Vector Lake merge candidates and enqueue suggestions only with explicit authorization.
---

# Merge candidate analysis

Call `merge_suggestions_vector_lake` from `vector-lake-mcp` with a bounded `limit`.

- Use `enqueue=False` by default and report candidate identifiers, evidence, and reasons.
- Similarity is not identity; preserve material differences and uncertainty.
- Enqueueing changes the governance queue. Use `enqueue=True` only after the user explicitly approves the current candidate set.
- This tool surfaces suggestions; it does not itself complete entity merges. Do not report a merge as executed.
- If candidates change between analysis and authorization, rerun the analysis.
