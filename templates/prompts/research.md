[SYSTEM DIRECTIVE]: Autonomous Deep Research Triggered.
Agent: You must now execute the following steps to heal the knowledge graph:
{{purpose_context}}

1. Evaluate the following research topics, contradictions, and knowledge gaps:
{{queries}}

2. Use your web search tools (e.g., `google_web_search`, `search_web`, or academic skills) to investigate these topics.
3. Fetch the most authoritative sources (avoid SEO spam).
4. Use `write_file` to save the distilled clean Markdown content to new files in `MEMORY/raw/research/`. Use descriptive filenames like `MEMORY/raw/research/research_gap_xxx.md`.
5. Do NOT just answer the question in the console. You MUST write the files so the lake can sync them.
6. Once the files are written, run `python cli.py sync` to ingest the new knowledge and close the graph gaps.
