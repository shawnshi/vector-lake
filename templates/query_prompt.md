[SUBAGENT DELEGATION REQUIRED]
Context successfully assembled and saved to: {{payload_path}}

Please execute the following workflow:
1. Invoke the subagent `vector-lake-synthesizer` with the exact prompt below.
2. Wait for the subagent to finish writing the file(s) (it must use its write_file tool).
3. Once the subagent finishes, find out which Synthesis_*.md files were created or modified.
4. Call the MCP tool `finalize_query_synthesis` with those filenames (comma-separated, e.g., 'Synthesis_A.md,Synthesis_B.md') and the original query string.

--- SUBAGENT PROMPT ---
Query: {{query_str}}

Instructions:
Read the context from {{payload_path}}. 
Perform bounded logical synthesis and generate the resulting Markdown synthesis page(s).
You MUST use the lazy MCP tool `call_mcp_tool` (ServerName="vector-lake-mcp", ToolName="write_wiki_page") to save your synthesis directly to the Wiki.
DO NOT use native `write_to_file`. Make sure the filename starts with `Synthesis_`.

[CRITICAL REQUIREMENT: GAP ANALYSIS]
You MUST include a section titled "## 盲区与缺失度分析 (Gap Analysis)" at the end of your synthesis.
In this section, explicitly state:
1. What crucial evidence is MISSING to definitively answer the query.
2. The staleness of the retrieved context.
3. Unresolved contradictions flagged in the Operational Memory warnings.
-----------------------
