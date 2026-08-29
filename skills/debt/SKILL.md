---
name: debt
metadata:
  version: 11.20.0
  tier: read-only
description: Retrieve and summarize current Vector Lake governance debt without changing the queue.
---

# Governance debt

Call `get_governance_debt` from `vector-lake-mcp`, using a bounded `top` value appropriate to the request.

- Preserve the tool's categories, counts, identifiers, and status vocabulary.
- Separate measured debt from interpretation; do not invent thresholds or trend claims without a comparable snapshot.
- This skill is read-only. Cleanup, queue updates, research, or memory persistence require a separate explicit request.
- If the response is incomplete or malformed, report that limitation instead of filling gaps.
