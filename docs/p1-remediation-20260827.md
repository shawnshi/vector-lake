# Vector Lake P1 remediation contract

This change set is bound to the read-only architecture audit performed on
2026-08-27 against the active package
`11.18.3+codex.20260826150723` and runtime revision
`25e3eaf71392fa0c16504eff61b6b527d739371d635d96226ba90f566aa0ce68`.
The exact active source baseline was imported as local commit `5712f56`.

Passing unit tests is necessary but does not close an item whose acceptance
evidence includes live state, migration, recovery, capacity, or service
identity.

| ID | Required outcome | Completion evidence |
|---|---|---|
| BIZ-01 | Semantic readiness and topology debt are measurable, bounded, and suitable for the declared medical-digital product scope. | Exact first-snapshot row/byte/graph/debt limits, generation/source-bound cached cursor pages with zero repeat scans, assessment coverage report, graph-generation-bound report, and explicit remaining human-review debt. |
| OPS-01 | The three terminal ingest jobs are reconciled and the authoritative Watchdog runs without the ledger-reconcile error. | Preview fingerprint, apply receipt, exact job/outbox counts, PID command-line identity, fresh component heartbeats, and post-restart soak. |
| EFF-01 | Operational-memory search never silently scans an unbounded canonical tail, accepts an unverified equal-count index, or leaves a corrupt derived index permanently ready. | Missing/orphan/equal-count/pending-loss/race/limit/v5-upgrade tests; revision-cached durable proof; strict batch-budget replay; and a fresh 126k run proving 1.816197s cold proof, 0.034392s hot query, and exactly 13 batches of 10,000 for repair. |
| STAB-01 | Provider responses are generation-CAS fenced, while persisted embeddings remain valid only when current node input, model, dimensions, and input contract match. Unrelated graph generations must not force a full-corpus re-embedding. | v8 migration tests, an in-flight canonical-update race proving stale responses are rejected, incremental A/B rebuild tests, and KNN crowding tests proving invalid nearer rows cannot cause false-empty recall. |
| STAB-02 | FTS completeness is generation-bound and a missing row cannot produce a false-green empty result. | Deliberate row-deletion test proving health failure or correct committed fallback, plus rebuild recovery. |
| STAB-03 | Transient generation/lease conflicts do not consume poison retry budget. | Three-conflict regression proving the event remains retryable, and deterministic-payload regression proving eventual dead-letter. |
| AUTH-01 | Every supported launcher resolves the enabled runtime authority; unsupported direct queue writers are absent. | Skill contract test, PID command-line proof, package inventory test, and old-checkout negative test. |
| UX-01 | Commands, skills, completion semantics, and host capabilities match the actual MCP registry without hidden-reasoning output. | Exhaustive command/skill contract validation and independent realistic skill forward tests. |
| PRIV-01 | Automatic ingest cannot send raw text to a model unless the effective config explicitly acknowledges that processing boundary. | Fail-closed config tests, enabled-path test, and redacted runtime capability/status output. |
| EFF-02 | Runtime identity checks are cached but bounded, and the fast lane has explicit bounded capacity and retry semantics. | Tamper-detection, single-flight, 1/1 compatibility, 2/4 saturation, and guard-latency tests. |
| EFF-03 | Unchanged raw inventories avoid full content reads, event bursts are truly debounced, and backup growth is quota-visible and preflight-gated. | Byte-read counters, scrub coverage, burst tests, quota planning, low-space preflight, and restore-guard tests. |
| REL-01 | Every shipped executable entrypoint is runnable and included in release quality gates; schema v8 has a receipt-bound v7 recovery path. | Ruff, compileall, entrypoint inventory test, full pytest, v7→v8→v7 database/projection round trip, verified complete/partial orphan-forward recovery after publication failures, fresh 11.18.3 Doctor/search/MCP acceptance, clean semantic diff, and package-manifest validation. |

## Deployment boundary

The candidate may be tested only against isolated roots until all migration and
rollback tests pass. Local plugin activation, live schema migration, ingest-job
reconciliation, Watchdog restart, or Git publication require their own recorded
receipt. Git publication remains out of scope unless separately authorized.
