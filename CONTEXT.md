# CONTEXT: Vector Lake

## 1. Active Position

Vector Lake is a Markdown-first knowledge compiler with a file-backed governance runtime. It should not be treated as a classic vector database or a stateless RAG service.

Current boundary:

- Human-facing memory: `MEMORY/wiki/*.md`
- Page runtime index: `MEMORY/wiki/index.json`
- Claim topology: `MEMORY/wiki/claim_graph.json`
- Canonical governance store: `MEMORY/wiki/.meta/*.json`
- Fallback governance store: `data/v8_meta/*.json`
- Agent runtime memory: `operational_memory.json`

The durable architecture is:

```text
raw source -> Markdown wiki -> canonical claims/evidence -> operational memory -> Memory Packet -> query context
```

## 2. Runtime Model

Markdown remains the sovereign, inspectable publication layer. Agent memory is compiled, scored, and selectively injected.

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
| `vector_lake/ingest.py` | Raw-source sync and two-step page generation |
| `vector_lake/indexer.py` | Page index, weighted edges, claim graph refresh |
| `vector_lake/claim_extractor.py` | Page-to-entity/claim/evidence/source extraction |
| `vector_lake/governance_store.py` | Canonical store, change sets, operational memory, conflict resolver |
| `vector_lake/governance_metrics.py` | Debt and health metrics |
| `vector_lake/tool_search.py` | Page search, memory search, claim search, Memory Packet assembly |
| `vector_lake/tool_query.py` | Query synthesis with Memory Packet first |
| `vector_lake/tool_review.py` | Unified review surface |
| `vector_lake/tool_doctor.py` | Runtime layout and dependency checks |
| `vector_lake/wiki_utils.py` | Path resolution, frontmatter, atomic writes, backups |
| `schema.md` | Wiki and runtime memory contract |
| `commands/` | Command-level operating prompts |
| `agents/` | Ingestor and synthesizer contracts |

## 4. CLI Contract

Use CLI commands as the ground truth operating surface.

```powershell
python cli.py doctor
python cli.py sync
python cli.py search "query" --top_k 5
python cli.py search "query" --mode memory --top_k 5
python cli.py search "query" --mode claim --top_k 5
python cli.py query "question" [--dry-run]
python cli.py review
python cli.py audit-graph
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

Last verified: 2026-04-30.

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
5. Treat lock contention as environmental state, not proof that a code patch failed.
6. Use `PYTHONUTF8=1` when scripts may print Chinese paths.
7. Never silently include unrelated dirty files in a publish or commit scope.
