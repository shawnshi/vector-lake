# CONTEXT: Vector Lake

## 1. Active Position

Vector Lake is a Markdown-first knowledge compiler with a file-backed governance runtime. It should not be treated as a classic vector database or a stateless RAG service.

Current boundary:

- Human-facing memory: `MEMORY/wiki/*.md`
- Page runtime index: `MEMORY/wiki/index.json`
- Claim topology: `MEMORY/wiki/claim_graph.json`
- Strategic intent: `MEMORY/purpose.md` (YAML contract parsed by `purpose_contract.py`)
- Canonical governance store: `MEMORY/wiki/.meta/vector_lake.db` (SQLite)
- Agent runtime memory: `operational_memory.json`

The durable architecture is:

```text
raw source -> Markdown wiki -> canonical claims/evidence -> operational memory -> Memory Packet -> query context
```

## 2. Runtime Model

Markdown remains the sovereign, inspectable publication layer. Agent memory is compiled, scored, and selectively injected. **(V7.2+ Mandate: All new operational memory MUST be persisted directly into Markdown Wiki nodes via the Dual-Schema layout, specifically under the `## 2. 证据时间线 (Evidence Timeline)` section to prevent index-rebuild data loss.)**

Operational memory types:

- `fact`
- `preference`
- `decision`
- `task_state`

Scoring fields:

- `confidence_score`
- `freshness_score`
- `authority_score`
- `importance_score`
- `reinforcement_score`
- `validity_factor`
- `memory_score`

Conflict rules:

- Explicit contradictions use `authority_score > confidence_score > updated_at`.
- Same-key `preference / decision / task_state` records use `updated_at > authority_score > confidence_score`.
- Superseded losers are hidden from default runtime retrieval.
- Unresolved ties stay `conflicted` and must remain visible in governance surfaces.

## 3. Module Map

| Module | Responsibility |
|---|---|
| `cli.py` | Root CLI shim |
| `vector_lake/cli_app.py` | Argument parsing and command dispatch |
| `vector_lake/tools.py` | Tool facade |
| `vector_lake/tool_ingest.py` | Raw-source scan and Subagent instructions generation |
| `vector_lake/indexer.py` | Page index, weighted edges, claim graph refresh, pure-Python BM25 inverted index |
| `vector_lake/claim_extractor.py` | Page-to-entity/claim/evidence/source extraction |
| `vector_lake/tool_memory.py` | Wiki-as-Database operational memory persistence via MCP |
| `vector_lake/governance_store.py` | Canonical store, change sets, operational memory, conflict resolver |
| `vector_lake/governance_metrics.py` | Debt and health metrics |
| `vector_lake/tool_search.py` | Hybrid search (LLM Query Expansion + BM25 + Graph Traversal), Memory Packet assembly |
| `vector_lake/tool_query.py` | Query synthesis with Memory Packet first |
| `vector_lake/tool_research.py` | Autonomous deep research and graph insight processing |
| `vector_lake/purpose_contract.py` | Purpose parsing, ingestion admission, SIR review, and synthesis proposal thresholds |
| `vector_lake/tool_review.py` | Unified review surface |
| `vector_lake/tool_doctor.py` | Runtime layout and dependency checks |
| `vector_lake/mcp_server.py` | Standard Model Context Protocol (MCP) server entrypoint |
| `vector_lake/watchdog_app.py` | Real-time ingest watcher, background job orchestration, scheduled auto-lint |
| `vector_lake/watchdog_status.py` | Status JSON telemetry broadcaster for the daemon |
| `vector_lake/wiki_utils.py` | Path resolution, frontmatter, atomic writes, backups |
| `vector_lake/db.py` | Legacy DB utils |
| `vector_lake/db_store.py` | SQLite connection pooling, schema initialization, and WAL settings |
| `vector_lake/defense_hook.py` | Pre-flight constraints and guardrails |
| `vector_lake/skeleton_parser.py` | Parsers for structural validation |
| `vector_lake/provenance.py` | Tracing entities to raw sources |
| `vector_lake/tool_piea.py` | PIEA entity schema interceptor |
| `vector_lake/tool_bulk_reconciliation.py` | Graph reconciliation |
| `vector_lake/yaml_utils.py` | YAML helpers |
| `scripts/community_clustering_daemon.py` | V9.0 Louvain Community Detection and blind-spot self-healing |
| `schema.md` | Wiki and runtime memory contract |
| `commands/` | Macro-level workflows (e.g. research/review) for Agents |
| `agents/` | Ingestor and synthesizer contracts |

## 4. CLI Contract & MCP Interface

**Note (v8.3+)**: Agents interact with the system entirely through the `vector_lake/mcp_server.py` MCP tools (e.g. `search_vector_lake`, `sync_vector_lake`).

**Gemini CLI Slash Commands**: Common functions have been mapped to slash commands (trigger with `/` in the chat input):
- `/vl_sync`: Distributed Subagent pipeline for graph sync and raw file ingestion
- `/search`: Semantic query
- `/query`: Deep logic reasoning
- `/review`: Check governance queue
- `/resolve`: Resolve pending items
- `/audit`: Synthesize topology and audit
- `/debt`: View governance debt metrics
- `/lint`: Self-healing audit of nodes
- `/research`: Autonomous web research directive
- `/graph`: Generate interactive 3D HTML topology
- `/doctor`: Validate runtime dependencies and health
- `/gc`: Garbage collect orphaned entities
- `/delete`: Cascade-delete sources and sever graph edges
- `/trace`: Audit provenance traces
- `/merge`: Surface candidate entity merges
- `/timeline`: SQL query against historical timeline_events (via MCP)
- `review_strategic_purpose(as_of="")`: emits due `SIR-Review-Proposal` records without mutating the Wiki.

The following CLI commands remain the ground truth operating surface for *human operators*:

```powershell
python cli.py doctor
python cli.py sync
python cli.py search "query" --top_k 5
python cli.py search "query" --mode memory --top_k 5
python cli.py search "query" --mode claim --top_k 5
python cli.py query "question" [--dry-run]
python cli.py review
python cli.py audit-graph
python cli.py research [--dry-run]
python cli.py debt --top 20
python cli.py trace "<query-or-id>"
python cli.py merge-suggestions --limit 20
python cli.py graph
python cli.py gc --days 30 --dry-run
python cli.py delete "<raw-source-path>" --dry-run
```

For Windows validation, prefer:

```powershell
$env:PYTHONUTF8='1'; python -m unittest discover -s tests -p 'test_*.py' -v
$env:PYTHONUTF8='1'; python -m compileall vector_lake tests
```

## 5. Current Validation Baseline

Last verified: 2026-07-08 (V11.5 Refactoring).

- Unit tests: `Ran 8 tests ... OK`
- Compile: `python -m compileall vector_lake tests` OK
- Doctor: healthy
- `search --mode memory`: smoke OK
- Debt snapshot:
  - `operational_memory_count: 13755`
  - `superseded_memory_count: 510`
  - `conflicted_memory_count: 0`
  - `memory_type_counts: {'fact': 11881, 'decision': 1393, 'task_state': 384, 'preference': 97}`

## 6. Operating Rules

1. Preserve the split: Markdown is for humans; `.meta` is canonical state; `operational_memory` is for Agents.
2. Keep `schema.md`, `README.md`, `commands/`, and `agents/` aligned when the runtime surface changes.
3. Do not hand-edit derived runtime files unless the task is explicitly data repair. Prefer rebuild paths.
4. Use dry-run first for delete, gc, and any operation that removes assets.
5. Treat lock contention as environmental state, not proof that a code patch failed. Note that `daemon_watchdog` and `sync` operations are protected by cross-process `filelock` to prevent meta and index corruption.
6. Use `PYTHONUTF8=1` when scripts may print Chinese paths.
7. Never silently include unrelated dirty files in a publish or commit scope.

## 7. System Capabilities & Architecture Defenses
The Vector Lake system is designed for high-concurrency ingestion and graph maintenance with several defensive mechanisms:
- **Two-Track Watchdog**: Monitors raw sources for incremental ingestion (via CoT LLM agents) and wiki nodes for O(1) index updates. Now correctly hooks `on_deleted` and `on_moved` events to instantly reflect Semantic GC operations and prevent "ghost nodes".
- **API Circuit Breaker**: Protects against quota exhaustion and rate-limit thrashing during concurrent LLM ingestion. Features an exponential backoff with jitter and a 10-minute cooldown blacklist for hard API errors (e.g., 404, quota limits).
- **I/O Debouncing**: The Indexer buffers multiple O(1) memory mutations (BM25 updates, edge recalculations) across batched file events and flushes them in a single write operation to `index.json`. This eliminates O(N) disk thrashing during heavy wiki modifications.
- **Lock-Free Auto-Lint**: Runs a highly-destructive, autonomous `lint_vector_lake(auto_fix=True)` daily at 10:00 AM and 23:00 PM. This offline routine merges duplicate nodes, archives decaying/contested pages (Semantic GC), and enforces schema compliance. Crucially, it executes purely deterministically without synchronous LLM calls, preventing hours-long thread locks.

The following CLI commands remain the ground truth operating surface for *human operators*:

```powershell
python cli.py doctor
python cli.py sync
python cli.py search "query" --top_k 5
python cli.py search "query" --mode memory --top_k 5
python cli.py search "query" --mode claim --top_k 5
python cli.py query "question" [--dry-run]
python cli.py review
python cli.py audit-graph
python cli.py research [--dry-run]
python cli.py debt --top 20
python cli.py trace "<query-or-id>"
python cli.py merge-suggestions --limit 20
python cli.py graph
python cli.py gc --days 30 --dry-run
python cli.py delete "<raw-source-path>" --dry-run
```

For Windows validation, prefer:

```powershell
$env:PYTHONUTF8='1'; python -m unittest discover -s tests -p 'test_*.py' -v
$env:PYTHONUTF8='1'; python -m compileall vector_lake tests
```

## 5. Current Validation Baseline

Last verified: 2026-07-08 (V11.5 Refactoring).

- Unit tests: `Ran 8 tests ... OK`
- Compile: `python -m compileall vector_lake tests` OK
- Doctor: healthy
- `search --mode memory`: smoke OK
- Debt snapshot:
  - `operational_memory_count: 13755`
  - `superseded_memory_count: 510`
  - `conflicted_memory_count: 0`
  - `memory_type_counts: {'fact': 11881, 'decision': 1393, 'task_state': 384, 'preference': 97}`

## 6. Operating Rules

1. Preserve the split: Markdown is for humans; `.meta` is canonical state; `operational_memory` is for Agents.
2. Keep `schema.md`, `README.md`, `commands/`, and `agents/` aligned when the runtime surface changes.
3. Do not hand-edit derived runtime files unless the task is explicitly data repair. Prefer rebuild paths.
4. Use dry-run first for delete, gc, and any operation that removes assets.
5. Treat lock contention as environmental state, not proof that a code patch failed. Note that `daemon_watchdog` and `sync` operations are protected by cross-process `filelock` to prevent meta and index corruption.
6. Use `PYTHONUTF8=1` when scripts may print Chinese paths.
7. Never silently include unrelated dirty files in a publish or commit scope.

## 7. System Capabilities & Architecture Defenses
The Vector Lake system is designed for high-concurrency ingestion and graph maintenance with several defensive mechanisms:
- **Two-Track Watchdog**: Monitors raw sources for incremental ingestion (via CoT LLM agents) and wiki nodes for O(1) index updates. Now correctly hooks `on_deleted` and `on_moved` events to instantly reflect Semantic GC operations and prevent "ghost nodes".
- **API Circuit Breaker**: Protects against quota exhaustion and rate-limit thrashing during concurrent LLM ingestion. Features an exponential backoff with jitter and a 10-minute cooldown blacklist for hard API errors (e.g., 404, quota limits).
- **I/O Debouncing**: The Indexer buffers multiple O(1) memory mutations (BM25 updates, edge recalculations) across batched file events and flushes them in a single write operation to `index.json`. This eliminates O(N) disk thrashing during heavy wiki modifications.
- **Lock-Free Auto-Lint**: Runs a highly-destructive, autonomous `lint_vector_lake(auto_fix=True)` daily at 10:00 AM and 23:00 PM. This offline routine merges duplicate nodes, archives decaying/contested pages (Semantic GC), and enforces schema compliance. Crucially, it executes purely deterministically without synchronous LLM calls, preventing hours-long thread locks.
- **Autonomous Sub-Daemons**: The lint loop silently orchestrates `metadata_decay_daemon.py` (TTL rot), `sync_timeline_db.py` (SQL timeline extraction), `missing_evidence_scout.py` (dispatching intelligence tasks to governance queue), **`semantic_dedup_daemon.py`** (pairwise semantic deduplication via Gemini embeddings), **`compile_domain_overviews.py`** (PageRank centrality compilations), and the V9.0 **`community_clustering_daemon.py`** (Louvain clustering to detect low-cohesion blind spots), culminating in a daily SQLite `WAL TRUNCATE` maintenance.
- **Strategic Intent Engine (V12.0)**: Parses `MEMORY/purpose.md` as the primary source for intent keywords and weighting. The same contract is injected into ingestion, retrieval, and research; it validates `strategic_scope` and `evidence_tier` during ingest, exposes SIR review proposals, and creates de-duplicated `Synthesis-Proposal` queue items only after the configured independent-source and tension thresholds are met.
- **Strict Two-Step CoT Ingestion (V9.0)**: Forces ingestion subagents to output an intermediate `analysis_buffer.json` (parsing tensions, consensus, unknowns) before writing Markdown, eliminating extraction omissions.
- **Strict 7-Type Enforcement (V9.1)**: The PIEA interceptor actively strips nested/invalid prefixes (e.g., `Concept_Synthesis_` or `Entity_`) and forces LLM agents to save files using exact, canonical 7-type filenames (`vendor`, `product`, `person`, `event`, `concept`, `synthesis`, `source`). All backend algorithms are strictly aligned to this matrix.
- **Cross-Type PIEA Deduplication (V9.1)**: `tool_piea.py` no longer segregates similarity checks by type. If an agent proposes `Vendor_Accenture` when `Concept_Accenture` already exists, it triggers a hard block and forces a timeline append, eradicating "Same Name, Multi-Type" pollution.
- **Error Resilience**: Utilizes a global `global_task_lock` for thread safety and halts on consecutive failures to prevent cascading storms. Status telemetry and subprocess errors are emitted to `MEMORY/wiki/.meta/.watchdog_status.json`.
- **AST-Based Parsing (V11.5)**: Replaced brittle Regex and string splitting with `mistune` Abstract Syntax Tree (AST) parsing, making extraction immune to Markdown stylistic variations or Markdown linting format changes.
- **Native Vector Engine (V11.5)**: Integrated `sqlite-vec` extension for FTS5 + Vector hybrid search. Eliminated the `embeddings.pkl` O(N) memory bottleneck, offloading similarity calculation directly into the SQLite C-backend.
- **O(V+E) Graph Indexing (V11.5)**: Eliminated catastrophic O(N²) CPU deadlocks during node overlapping frequency calculations by utilizing an inverted-index map. 
- **Chinese Tokenization (V11.5)**: Implemented offline `jieba` pre-tokenization pipeline before SQLite `MATCH` execution, fixing the precision drop caused by `porter unicode61` character splitting.
- **Subagent Concurrency Shield (V11.5)**: Migrated SDK LLM calls to Antigravity native subagents (`agy -p`) with `asyncio.Semaphore` physical throttling to entirely eliminate "process storms" and EOF rate limits.
- **Cross-Storage Transactions (V11.5)**: Secured SQLite database writes and JSON file index updates within `db_store.transaction()` two-phase commit blocks and introduced exponential backoff for file locks.
- **Pure Canonical Architecture & Outbox (V11.11)**: Decoupled derived layer indexing from fragile file reads. Mutations from all bypasses (MCP, Bulk Merge, Garbage Collection, Stub generation) converge on a single `mutation_coordinator`, applying Markdown updates, SQLite canonical transactions, and Outbox persistence (`mutation_outbox`) in absolute atomicity. `generate_index` operates purely off the SQLite `entities` table, rendering dirty Markdown files incapable of polluting the logical knowledge graph.
