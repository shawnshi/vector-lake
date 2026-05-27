---
name: check_duplicate
description: Check if an entity or concept already exists in the graph to prevent duplicates before creation.
---
Please use the `check_duplicate_entity` MCP tool from the `vector-lake-mcp` server.

Always use this tool BEFORE you write a new Entity/Concept Markdown file to the Vector Lake directory.
Provide the `candidate_title`, `candidate_type` (e.g. 'vendor', 'product', 'person', 'event', 'concept'), and an optional `candidate_summary` to perform a fast token-based similarity match against the live governance store.

If the tool returns a highly similar existing entity, you should append to the existing entity's Evidence Timeline rather than creating a duplicate.
