# Commands Layout

`commands/` is grouped by operational intent rather than historical naming.

## Core Workflow

- `sync.toml`: compile raw sources into wiki nodes
- `search.toml`: query the runtime index with optional filters
- `query.toml`: synthesize answers and optionally dry-run output
- `lint.toml`: run wiki and governance health checks
- `graph.toml`: render the interactive page/claim topology
- `review.toml`: inspect and resolve the unified legacy/governance review surface
- `delete.toml`: preview or execute raw-source cascade deletion
- `doctor.toml`: validate runtime dependencies and filesystem layout

## Governance Workflow

- `governance_migrate.toml`: backfill canonical V8 governance objects
- `governance_publish.toml`: publish pending change sets
- `governance_debt.toml`: show the governance debt dashboard
- `governance_trace.toml`: inspect provenance for a query or identifier
- `governance_merge.toml`: preview or enqueue entity merge suggestions
- `governance_audit.toml`: audit graph topology gaps into the review queue

## Background Services

- `daemon_watchdog.toml`: launch the background ingest watcher

## Review Semantics

`review.toml` fronts the CLI `review` command, which now merges:

- legacy `review_queue.json`
- V8 `governance_queue.json`

Operators can resolve items either by the visible pending index or by stable `item_id`.

## Renamed Commands

- `sync_vector_lake.toml` -> `sync.toml`
- `search_vector_lake.toml` -> `search.toml`
- `query_logic_lake.toml` -> `query.toml`
- `run_wiki_lint.toml` -> `lint.toml`
- `show_graph.toml` -> `graph.toml`
- `review_queue.toml` -> `review.toml`
- `delete_source.toml` -> `delete.toml`
- `doctor_vector_lake.toml` -> `doctor.toml`
- `migrate_v8.toml` -> `governance_migrate.toml`
- `publish_vector_lake.toml` -> `governance_publish.toml`
- `debt_vector_lake.toml` -> `governance_debt.toml`
- `trace_vector_lake.toml` -> `governance_trace.toml`
- `merge_suggestions_vector_lake.toml` -> `governance_merge.toml`
- `watchdog_sync.toml` -> `daemon_watchdog.toml`
