# Commands Layout

`commands/` contains Gemini CLI slash-command compatibility prompts. Codex plugins do not load this directory as slash commands; Codex exposes the equivalent workflows through namespaced skills such as `$vector-lake:query` and `$vector-lake:timeline`.

> **Note**: Vector Lake 8.3+ uses MCP for execution. These `.toml` files only translate Gemini CLI slash commands into calls to the MCP tools in `vector_lake/mcp_server.py`.

## Macro Workflows

- `research.toml`: autonomously orchestrates the graph gap scanning, web research loop, and subsequent synchronization.
- `review.toml`: inspects and resolves the unified governance review surface using the MCP backend.

## Query Workflows

- `query.toml`: maps `/query` to `query_logic_lake` in Gemini CLI.
- `timeline.toml`: maps `/timeline` to `search_timeline` in Gemini CLI.

## Background Services

- `daemon_watchdog.toml`: launch the background ingest watcher.
