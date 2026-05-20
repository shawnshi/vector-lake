---
name: delete
description: Cascade-delete a raw source and all related wiki pages.
---
Please use the `delete_source` MCP tool from the `vector-lake-mcp` server to remove a source file and automatically sever its graph edges. You MUST pass the `raw_path` argument. Use `dry_run=True` first if you want to preview the blast radius.
