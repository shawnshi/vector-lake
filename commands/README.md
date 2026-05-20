# Commands Layout

`commands/` is reserved for macro-level workflows that orchestrate multiple Agent steps or launch background services.

> **Note**: Vector Lake 8.3+ has migrated entirely to the Model Context Protocol (MCP). All low-level CRUD operations (sync, query, lint, trace, delete) are now directly exposed to the Agent as MCP Tools via `vector_lake/mcp_server.py`. They no longer require `.toml` prompt macros.

## Macro Workflows

- `research.toml`: autonomously orchestrates the graph gap scanning, web research loop, and subsequent synchronization.
- `review.toml`: inspects and resolves the unified governance review surface using the MCP backend.

## Background Services

- `daemon_watchdog.toml`: launch the background ingest watcher.
