---
name: research
description: 'Autonomously scan graph gaps and the governance queue to formulate web research directives.'
---
Please use the `trigger_autonomous_research` MCP tool from the `vector-lake-mcp` server to scan the graph topology and governance queue for knowledge gaps.

If a directive is emitted, you (the Main Agent) MUST use the `invoke_subagent` tool to delegate this to a `research` subagent (Role: `Autonomous Researcher`). 
Pass the directive into the subagent's Prompt and instruct it to:
1. Execute the required web searches using `search_web`.
2. Save the results to `C:\Users\shich\.gemini\MEMORY\raw\research\`.
3. Use the `sync_vector_lake` MCP tool to ingest the findings.

The Main Agent MUST NOT execute this heavy flow synchronously.

If you are asked to dry-run the research command, pass `dry_run=True` to the MCP tool.
