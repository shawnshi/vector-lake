---
name: review
description: 'Inspect the unified V8 governance queue for contradictions, topology gaps, merge suggestions, and knowledge debt items.'
---
Please use the `review_governance_list` MCP tool from the `vector-lake-mcp` server to list all pending review items.

When presenting the result:
- call out the visible pending index
- preserve the stable item_id shown by the tool

If the user asks to resolve an item, use the `resolve_governance_item` MCP tool.
If an item points to a missing page gap, you can suggest using the `trigger_autonomous_research` MCP tool to fetch real internet data to fill the gap.
