---
name: sync
description: 'Trigger a 2-step CoT sync of all raw sources into the Vector Lake Wiki nodes.'
---
**[Loop Engineering Protocol]** 严禁主代理直接同步调用极重算力的入湖工具。
You MUST use the `invoke_subagent` tool to spawn a `self` subagent (Role: `vector-lake-ingestor`) and pass it the prompt: "Please use the `sync_vector_lake` MCP tool from the `vector-lake-mcp` server to ingest all pending raw sources into the knowledge graph."
The main agent MUST NOT wait for the subagent synchronously; it should release control immediately.
