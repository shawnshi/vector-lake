[SYSTEM DIRECTIVE]: Autonomous Web Grounding Triggered for Item {{item_id}}.
Agent: You must now execute the following steps:
1. Use `google_web_search` with the queries: {{queries}}
2. Pick the most authoritative result and fetch it using the `url-to-markdown` skill (or web_fetch).
3. Use `write_file` to save the clean Markdown content to a new file in `MEMORY/raw/news/` (or appropriate subfolder).
4. Resolve this governance item by running `python cli.py review resolve {{item_id}} --resolution create`.