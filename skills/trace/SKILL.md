---
name: trace
metadata:
  version: 11.20.0
  tier: read-only
description: Trace the provenance of a Vector Lake query or stable identifier without changing the graph.
---

# Provenance trace

Call `trace_vector_lake` from `vector-lake-mcp` with the exact `query_or_id` supplied or confirmed by the user.

- Preserve source identifiers, paths, hashes, revisions, timestamps, and relation direction returned by the tool.
- Distinguish direct provenance from inferred relationships and explicitly mark gaps.
- Do not fabricate missing lineage, repair links, or update memory during a trace.
- If the identifier is ambiguous, present the candidates and ask the user to select one before tracing further.
