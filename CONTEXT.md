# CONTEXT: Vector Lake

## 1. Active Position

Vector Lake is a local knowledge compiler with an inspectable Markdown publication surface and a SQLite canonical runtime. It should not be treated as a classic vector database, a stateless RAG service, or a CBSS business execution runtime.

The supported deployment is a controlled Windows/Codex, single-user, expert-operated healthcare-digitalization research workbench. It is not an enterprise multi-tenant service or an unattended GA system. Concurrency claims below refer only to bounded workers inside one trusted local runtime.

Current boundary:

- Human-facing memory: `MEMORY/wiki/*.md`
- Page runtime index locator: `MEMORY/wiki/index.json`
- Claim topology locator: `MEMORY/wiki/claim_graph.json`
- Immutable projection objects: `MEMORY/wiki/.projection-store/objects/sha256/`
- Strategic intent: `MEMORY/purpose.md` (YAML contract parsed by `purpose_contract.py`)
- Canonical governance store: `MEMORY/wiki/.meta/vector_lake.db` (SQLite)
- Agent runtime read model: SQLite `operational_memory` table, compiled from canonical claims

The durable architecture is:

```text
raw source -> page-scoped coordinator -> SQLite canonical + fenced outbox -> Markdown + projection-v2 roots/locators
SQLite canonical -> operational memory -> Memory Packet -> query context
```

CBSS boundary:

- Vector Lake owns Source, Evidence, Claim candidates, provenance, knowledge projections, and retrieval context.
- CBSS owns authority acceptance, AcceptedFact lifecycle, Aggregate state, Command, executable Policy, Decision, ActionRequest, ExecutionResult, business Event Ledger, compensation, and System-of-Record reconciliation.
- `contracts/cbss/` defines the transfer boundary. Vector Lake Timeline and `memory_type=decision` are not CBSS business records.

## 2. Runtime Model

Markdown remains the inspectable publication layer. SQLite is the transactional canonical layer. New operational memory enters through the coordinator and preserves its Markdown evidence timeline so projections can be rebuilt without applying synthesized restore text back into canonical state.

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
| `vector_lake/tool_memory.py` | Governed operational-memory observation persistence via MCP |
| `vector_lake/memory_protocol.py` | Stable Agent-memory verbs and bounded thin-client adapters |
| `vector_lake/retrieval_benchmark.py` | Read-only, dataset-hash-bound retrieval evaluation |
| `vector_lake/governance_store.py` | Canonical store, change sets, operational memory, conflict resolver |
| `vector_lake/governance_metrics.py` | Debt, health metrics, and merge-candidate report orchestration |
| `vector_lake/merge_analysis.py` | Unicode-safe duplicate recall, evidence scoring, four-state decisions, component grouping, and merge preflight |
| `vector_lake/tokenizer_runtime.py` | Shared rjieba boundary for consistent index and query tokenization |
| `vector_lake/tool_search.py` | Exact identity + local query expansion + BM25 + Graph Traversal, Memory Packet assembly |
| `vector_lake/tool_query.py` | Query synthesis with Memory Packet first |
| `vector_lake/tool_research.py` | Autonomous deep research and graph insight processing |
| `vector_lake/purpose_contract.py` | Purpose parsing, ingestion admission, SIR review, and synthesis proposal thresholds |
| `vector_lake/tool_review.py` | Unified review surface |
| `vector_lake/tool_doctor.py` | Infrastructure checks and separate semantic-readiness report |
| `vector_lake/runtime_health.py` | Read-only infrastructure-health and semantic-readiness evaluators |
| `vector_lake/diagnostic_snapshot.py` | Shared as-of snapshot and cross-surface drift fence |
| `vector_lake/tool_evidence.py` | Read-only EvidencePacket export by claim ID |
| `vector_lake/evidence_foundation.py` | SourceArtifact integrity, raw locators, extraction runs, and lineage flags |
| `vector_lake/claim_assessment.py` | Append-only claim assessments without AcceptedFact promotion |
| `vector_lake/decision_registry.py` | Verified external decision registry adapter and scoped readiness |
| `vector_lake/quality_registry.py` | Immutable schema versions and quality-evaluation ledger |
| `vector_lake/mcp_server.py` | Standard Model Context Protocol (MCP) server entrypoint |
| `vector_lake/watchdog_app.py` | Real-time ingest watcher, background job orchestration, scheduled auto-lint |
| `vector_lake/watchdog_status.py` | Status JSON telemetry broadcaster for the daemon |
| `vector_lake/wiki_utils.py` | Path resolution, frontmatter, atomic writes, backups |
| `vector_lake/db_store.py` | SQLite connection pooling, schema initialization, and WAL settings |
| `vector_lake/projection_store_v2.py` | Immutable content-addressed HAMT object store |
| `vector_lake/projection_format_v2.py` | Root/locator/sidecar publication, materialization, and recovery delegates |
| `vector_lake/restore_snapshot.py` | Receipt-bound database/projection/Wiki recovery |
| `vector_lake/cancellation.py` | Cooperative deadlines and observable atomic completion |
| `vector_lake/durability.py` | File/directory durability profiles and persistence barriers |
| `vector_lake/mutation_coordinator.py` | Canonical transaction and fenced projection-outbox boundary |
| `vector_lake/defense_hook.py` | Pre-flight constraints and guardrails |
| `vector_lake/skeleton_parser.py` | Parsers for structural validation |
| `vector_lake/provenance.py` | Tracing entities to raw sources |
| `vector_lake/tool_piea.py` | PIEA entity schema interceptor |
| `vector_lake/tool_bulk_reconciliation.py` | Graph reconciliation |
| `vector_lake/yaml_utils.py` | YAML helpers |
| `scripts/community_clustering_daemon.py` | Deprecated/unsupported legacy Louvain operator script; disabled by default and never scheduled by watchdog |
| `schema.md` | Wiki and runtime memory contract |
| `commands/` | Macro-level workflows (e.g. research/review) for Agents |
| `contracts/cbss/` | Evidence, authority-acceptance, business-event, decision-registry, and readiness contracts |

`scripts/semantic_dedup_daemon.py` and `scripts/community_clustering_daemon.py`
are deprecated, unsupported, and fail closed before DB, index, or governance
access. A trusted operator may opt in only with
`VECTOR_LAKE_ENABLE_LEGACY_UNSAFE_DAEMONS=1` during isolated recovery; neither
script may run concurrently with `watchdog_sync.py` or
`vector_lake.watchdog_app.py`. Supported paths are watchdog/indexer topology
maintenance plus the preview-first `projection-rebuild-index`,
`embedding-backfill`, and `topology-queue-cleanup` CLI commands.

## 4. CLI Contract & MCP Interface

**Note (v8.3+)**: Agents interact with the system entirely through the `vector_lake/mcp_server.py` MCP tools (e.g. `search_vector_lake`, `sync_vector_lake`).

**Command surfaces**: Gemini CLI loads compatibility prompts from `commands/*.toml` and exposes them with `/`. Codex does not load plugin-defined slash commands; invoke the corresponding plugin skills with `$vector-lake:<name>` or ask the agent to call the MCP tool directly.

Gemini CLI compatibility commands:
- `/sync`: Scan configured raw sources and enqueue a bounded ingest batch; it does not generate or finalize Wiki pages
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
- `semantic_readiness(decision_id="")`: reports global semantic debt or, with a verified registry ID, only evidence and governance mapped to that decision; it does not change write-gate behavior.
- `export_evidence_packet(claim_id, include_evidence_text=False, max_evidence_text_chars=2000)`: exports a claim candidate and its provenance without accepting it as fact.
- `sync_critical_decision_registry(payload_file, expected_sha256, actor_id)`: imports only a sandboxed registry snapshot pinned by an operator-supplied SHA-256 digest and records the import receipt.
- `operational_memory_cleanup(dry_run=True, limit=0)` and `topology_queue_cleanup(dry_run=True)`: preview-first remediation surfaces for generated runtime artifacts and obsolete indexer naming work.

Codex equivalents include `$vector-lake:query` and `$vector-lake:timeline`.

The following CLI commands remain the ground truth operating surface for *human operators*:

```powershell
python cli.py doctor
python cli.py readiness
python cli.py evidence-packet "<claim_id>"
python cli.py sync
python cli.py search "query" --top_k 5
python cli.py search "query" --mode memory --top_k 5
python cli.py search "query" --mode fact --top_k 5
python cli.py retrieval-benchmark "dataset.json"
python cli.py query "question" [--dry-run|--apply]
python cli.py review
python cli.py audit-graph
python cli.py research [--dry-run|--apply]
python cli.py debt --top 20
python cli.py trace "<query-or-id>"
python cli.py merge-suggestions --limit 20
python cli.py graph
python cli.py gc --days 30 --dry-run
python cli.py delete "<raw-source-path>" --dry-run
python cli.py memory-cleanup
python cli.py topology-queue-cleanup
python cli.py schema-migrate
python cli.py schema-rollback --migration-receipt "<absolute-completed-receipt>"
python cli.py restore-snapshot --maintenance-receipt "<absolute-backup-manifest.json>"
python cli.py projection-object-gc --retention-days 7 --limit 1000
```

The current database contract is schema v9. Physical projection format v2 uses
small locators plus a sidecar commit pointer over immutable content-addressed
objects; its materialized logical payload remains projection contract v1.
`schema-rollback` accepts only an authoritative completed v8-to-v9 receipt.
`restore-snapshot` and `projection-object-gc` are preview/fingerprint/apply
maintenance surfaces and are intentionally CLI-only.

`search --mode fact` returns only `memory_type=fact` operational-memory rows.
Legacy `--mode claim` is a deprecated compatibility alias for `fact`, not a
canonical Claim query, and must surface that actual semantic to callers. Use
`evidence-packet` when a canonical Claim candidate and its provenance are needed.

Direct page/memory/fact search and the `recall`, `synthesize`, and `context_pack`
memory verbs expose `vector-lake-semantic-readiness-envelope/v1`. The envelope
contains bounded issues, warnings, and debt; the captured canonical, governance,
and projection generation/fingerprint; and
`results_are_not_accepted_facts=true`. A `not_ready`, `degraded`, or `unknown`
envelope is advisory and never suppresses the base retrieval result. The hot path
checks a lightweight generation token on every call and reuses a full assessment
for at most five seconds while that token remains stable. Any token change
invalidates immediately; an unverified binding or mid-assessment drift reports
`unknown` rather than a false `ready`.

For Windows validation, prefer:

```powershell
$env:PYTHONIOENCODING='utf-8'; python -m pytest -q -p no:cacheprovider
$env:PYTHONIOENCODING='utf-8'; python -m compileall -q vector_lake tests
```

## 5. Current Validation Baseline

The checked baseline is produced by the current CI commands rather than a fixed test or data count. Run the full pytest suite with warnings promoted to errors, `pip check`, `git diff --check`, read-only lint, and deep doctor before release. Runtime data counts are diagnostic snapshots and must not be copied into this contract as permanent expectations.

## 6. Operating Rules

1. Preserve the split: Markdown is for humans; `.meta` is canonical state; `operational_memory` is for Agents.
2. Keep `schema.md`, `README.md`, `commands/`, and `contracts/` aligned when the runtime surface changes.
3. Do not hand-edit derived runtime files unless the task is explicitly data repair. Prefer rebuild paths.
4. Use preview first for delete, gc, retention, restore, schema change, and any operation that removes or replaces assets.
5. Treat lock contention as environmental state, not proof that a code patch failed. Note that `daemon_watchdog` and `sync` operations are protected by cross-process `filelock` to prevent meta and index corruption.
6. Use `PYTHONUTF8=1` when scripts may print Chinese paths.
7. Never silently include unrelated dirty files in a publish or commit scope.
8. Keep infrastructure health and semantic readiness separate: the first protects mutations and projections; the second reports evidence/governance fitness to consumers.
9. Governance decision relevance must use explicit `critical_decision_refs`; never infer it from title or description text.
10. Do not use Vector Lake Timeline, `Policy_*` pages, or operational-memory decisions as CBSS Event, executable Policy, or Decision records.
11. `VECTOR_LAKE_MCP_SURFACE=memory` is an exact 9-tool thin surface that includes the governed `remember` mutation and read-only automatic-ingest budget status. `VECTOR_LAKE_MCP_SURFACE=readonly` is an exact 21-tool physical-read surface backed by SQLite `mode=ro` and `query_only`; its scan-class heavy tools remain bounded by the dedicated executor but bypass the canonical-meta file gate. It is not an operating-system ACL, so snapshot/generation drift still fails closed and independent read-only snapshots remain preferable for forensic audits. CLI diagnostics and the other MCP surfaces may still publish heavy-task lock/status telemetry.

## 7. System Capabilities & Architecture Defenses
The Vector Lake system uses bounded single-host concurrency for ingestion and graph maintenance, with several defensive mechanisms:
- **Two-Track Watchdog**: Raw changes are path-scoped and coalesced through one worker; Wiki changes enter a bounded legacy queue and are promoted through the coordinator.
- **Write Health Gate**: Ordinary mutations run deep key-and-content parity checks across Wiki, index, and canonical state. Drift on settled projections blocks writes. An active outbox row is treated as managed recovery only when its payload version exactly matches canonical state. Bounded repairs can use schema mode or an explicit operator override. Semantic readiness is read-only and does not alter this gate.
- **Fenced Outbox**: Claims carry owner, token, and generation. Same-page newer intents supersede older active rows without deleting history, and workers revalidate before materializing Markdown and before indexing.
- **Row-Level Governance Queue**: Enqueue, deduplication, publish, and resolve update only their target rows; unrelated concurrent items are preserved.
- **Incremental Projection Store**: The Indexer updates immutable HAMT paths for changed page/search/topology components and a bounded 512-node candidate frontier, then atomically advances the sidecar/DB publish state. Single-page writes no longer rewrite the full projection; unchanged generations are byte- and mtime-idempotent.
- **Scheduled Read-Only Lint**: At 10:00 and 23:00 the watchdog refreshes dirty graph topology and runs `lint_vector_lake(auto_fix=False)`. It does not checkpoint the SQLite WAL; WAL truncation remains a fingerprint-bound explicit maintenance action, and destructive repair remains an explicit operator action.
