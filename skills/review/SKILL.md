---
name: review
metadata:
  version: 11.20.0
  tier: read-only
description: List and inspect current Vector Lake governance items without resolving or researching them.
---

# Governance queue review

Call `review_governance_list` from `vector-lake-mcp`.

- Preserve both the visible index and stable `item_id` for every item.
- Group contradictions, topology gaps, merge suggestions, and other debt without changing their status.
- Report evidence, owner, due state, and missing context when present; do not invent absent fields.
- This skill is read-only. Resolution, autonomous research, merge enqueueing, and memory updates require separate explicit authorization.
- If the queue changes during review, identify the snapshot limitation.
