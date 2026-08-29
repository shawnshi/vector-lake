---
name: timeline
metadata:
  version: 11.20.0
  tier: read-only
description: Search stored Vector Lake timeline events by entity, sentiment, or action.
---

# Timeline search

Call `search_timeline` from `vector-lake-mcp` with supported filters: `entity_name`, `sentiment`, `action`, and a bounded `limit`.

- Leave a filter empty when it was not supplied; do not convert free-form dates into unsupported parameters.
- Preserve event timestamps, identifiers, actions, and sentiment exactly as returned.
- Sort or narrate only from observed timestamps.
- Do not infer causality from chronology alone, and disclose missing or ambiguous dates.
- This skill is read-only and must not add timeline events.
