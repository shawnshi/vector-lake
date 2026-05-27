---
name: memory_update
description: Safely persist an operational memory (preference, decision, fact, task_state) into the Vector Lake knowledge graph.
---
Please use the `update_operational_memory` MCP tool from the `vector-lake-mcp` server.

Use this tool when you need to permanently record:
1. `preference`: The user's implicit or explicit style/workflow preferences.
2. `decision`: A significant system or architectural decision that has been finalized.
3. `task_state`: The checkpoint of a long-running subagent or overarching project.
4. `fact`: An immutable operational fact discovered during runtime.

Provide the `memory_type` (one of the above) and the string `content` to the tool. Do NOT use standard filesystem tools (like write_to_file) to write to the operational memory directly; always route it through this tool so that the Dual-Schema layout is preserved.
