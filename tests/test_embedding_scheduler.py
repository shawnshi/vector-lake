import json
import sqlite3
import threading
import time
from contextlib import contextmanager
import pytest

from vector_lake import db_store, governance_store, indexer
from types import SimpleNamespace

from filelock import FileLock

from vector_lake import embedding_scheduler
from vector_lake.cancellation import (
    CancellationOperation,
    CooperativeCancellation,
    bind_cancellation_operation,
)
from vector_lake.embedding_scheduler import embedding_backfill, estimate_embedding_tokens
from vector_lake.mutation_coordinator import execute_mutation_plan


def _purpose(memory_dir):
    (memory_dir / "purpose.md").write_text(
        """---
purpose_version: "12.1"
intent_keywords: [test]
scope:
  core: [test]
  edge: [edge]
  excluded: [excluded]
  marketing_noise: [noise]
evidence_tiers:
  primary: Primary evidence
sir_registry:
  - id: SIR_TEST
    status: active
    review_after: 2099-01-01
    signal_keywords: [test]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Test purpose.
""",
        encoding="utf-8",
    )


def _source_content(entity_id: str, title: str):
    return f"""---
id: {entity_id}
title: {title}
type: source
domain: General
status: Active
epistemic-status: seed
categories: [Uncategorized]
updated: 2026-07-13T00:00:00+00:00
sources: [raw/test.pdf]
strategic_scope: core
evidence_tier: primary
---
Primary source content.
"""


def _bind_current_generation(index_data):
    result = dict(index_data)
    result["projection_manifest"] = {
        "generation": "embedding-test-generation",
        "canonical_generation": {
            "status": "verified",
            "runtime_generations": indexer.canonical_runtime_generation_snapshot(),
        },
    }
    return result


def _current_embedding_metadata(index_data, node_key="Concept_A"):
    from vector_lake.search_projection_contract import (
        EMBEDDING_INPUT_CONTRACT,
        embedding_content_sha256,
        verified_projection_runtime_generations,
    )

    config = embedding_scheduler.load_embedding_rate_config()
    node = index_data["nodes"][node_key]
    return config, {
        "content_sha256": embedding_content_sha256(
            node,
            max_chars=config.max_chars_per_item,
        ),
        "input_contract": EMBEDDING_INPUT_CONTRACT,
        "model": config.model,
        "dimension": config.dimension,
        "canonical_generation_json": json.dumps(
            verified_projection_runtime_generations(index_data),
            sort_keys=True,
        ),
    }


def _embedding_batch_records(index_data, *node_keys):
    config = embedding_scheduler.load_embedding_rate_config()
    expected = index_data["projection_manifest"]["canonical_generation"][
        "runtime_generations"
    ]
    records = []
    for node_key in node_keys:
        _, metadata = _current_embedding_metadata(index_data, node_key)
        records.append(
            {
                "entity_id": node_key,
                "embedding": [1.0] * config.dimension,
                "content_sha256": metadata["content_sha256"],
            }
        )
    return config, expected, records


def test_embedding_batch_has_one_transaction_and_generation_check(
    isolated_memory,
):
    db_store.init_db()
    index_data = _bind_current_generation(
        {
            "nodes": {
                "Concept_A": {"title": "A", "summary": "alpha"},
                "Concept_B": {"title": "B", "summary": "beta"},
            }
        }
    )
    config, expected, records = _embedding_batch_records(
        index_data,
        "Concept_A",
        "Concept_B",
    )
    statements = []
    conn = db_store.get_vector_connection()
    conn.set_trace_callback(statements.append)
    try:
        outcome = db_store.upsert_embeddings_if_current_batch(
            records,
            input_contract=embedding_scheduler.EMBEDDING_INPUT_CONTRACT,
            model=config.model,
            dimension=config.dimension,
            expected_runtime_generations=expected,
        )
    finally:
        conn.set_trace_callback(None)

    normalized = [" ".join(statement.upper().split()) for statement in statements]
    assert outcome == "written"
    assert sum(statement == "BEGIN IMMEDIATE" for statement in normalized) == 1
    assert sum(statement == "COMMIT" for statement in normalized) == 1
    assert (
        sum(
            "SELECT SURFACE, GENERATION FROM RUNTIME_GENERATIONS" in statement
            for statement in normalized
        )
        == 1
    )
    assert db_store.count_embeddings() == 2
    assert (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM embedding_metadata_v8")
        .fetchone()[0]
        == 2
    )


def test_embedding_batch_rolls_back_every_item_when_later_metadata_write_fails(
    isolated_memory,
):
    db_store.init_db()
    index_data = _bind_current_generation(
        {
            "nodes": {
                "Concept_A": {"title": "A", "summary": "alpha"},
                "Concept_B": {"title": "B", "summary": "beta"},
            }
        }
    )
    config, expected, records = _embedding_batch_records(
        index_data,
        "Concept_A",
        "Concept_B",
    )
    with db_store.transaction():
        db_store.get_connection().execute(
            "CREATE TRIGGER fail_second_embedding_metadata "
            "BEFORE INSERT ON embedding_metadata_v8 "
            "WHEN NEW.entity_id = 'Concept_B' BEGIN "
            "SELECT RAISE(ABORT, 'injected embedding batch failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected embedding batch failure"):
        db_store.upsert_embeddings_if_current_batch(
            records,
            input_contract=embedding_scheduler.EMBEDDING_INPUT_CONTRACT,
            model=config.model,
            dimension=config.dimension,
            expected_runtime_generations=expected,
        )

    assert db_store.count_embeddings() == 0
    assert (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM embedding_metadata_v8")
        .fetchone()[0]
        == 0
    )


def test_embedding_batch_discards_every_item_after_generation_drift(
    isolated_memory,
):
    db_store.init_db()
    index_data = _bind_current_generation(
        {
            "nodes": {
                "Concept_A": {"title": "A", "summary": "alpha"},
                "Concept_B": {"title": "B", "summary": "beta"},
            }
        }
    )
    config, expected, records = _embedding_batch_records(
        index_data,
        "Concept_A",
        "Concept_B",
    )
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE runtime_generations SET generation = generation + 1 "
            "WHERE surface = 'entities'"
        )

    outcome = db_store.upsert_embeddings_if_current_batch(
        records,
        input_contract=embedding_scheduler.EMBEDDING_INPUT_CONTRACT,
        model=config.model,
        dimension=config.dimension,
        expected_runtime_generations=expected,
    )

    assert outcome == "stale"
    assert db_store.count_embeddings() == 0
    assert (
        db_store.get_connection()
        .execute("SELECT COUNT(*) FROM embedding_metadata_v8")
        .fetchone()[0]
        == 0
    )


def test_embedding_coverage_uses_node_input_not_global_generation(
    isolated_memory,
):
    db_store.init_db()
    initial_index = _bind_current_generation(
        {
            "nodes": {
                "Concept_A": {"title": "A", "summary": "alpha"},
                "Concept_B": {"title": "B", "summary": "beta"},
            }
        }
    )
    config, metadata_a = _current_embedding_metadata(initial_index, "Concept_A")
    _, metadata_b = _current_embedding_metadata(initial_index, "Concept_B")
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE runtime_generations SET generation = generation + 1 "
            "WHERE surface = 'entities'"
        )
    current_index = _bind_current_generation(
        {
            "nodes": {
                "Concept_A": {"title": "A", "summary": "alpha changed"},
                "Concept_B": {"title": "B", "summary": "beta"},
            }
        }
    )

    coverage = embedding_scheduler.embedding_coverage(
        current_index,
        inventory={"Concept_A": metadata_a, "Concept_B": metadata_b},
        config=config,
    )

    assert coverage["embedded"] == 1
    assert coverage["missing"] == 1
    assert coverage["stale"] == 1
    assert coverage["content_stale"] == 1
    assert coverage["generation_provenance_drift"] == 2


@pytest.mark.parametrize(
    "generation_binding",
    [
        "",
        "{",
        json.dumps(
            {
                "entities": 0,
                "claims": 0,
                "sources": 0,
                "page_graph_edges": 0,
            }
        ),
        json.dumps(
            {
                "entities": 0,
                "claims": 0,
                "sources": 0,
                "page_graph_edges": 0,
                "claim_graph_edges": 0,
                "unexpected": 0,
            }
        ),
    ],
)
def test_embedding_coverage_treats_generation_metadata_as_audit_provenance(
    isolated_memory,
    generation_binding,
):
    db_store.init_db()
    index_data = _bind_current_generation(
        {"nodes": {"Concept_A": {"title": "A", "summary": "alpha"}}}
    )
    config, metadata = _current_embedding_metadata(index_data)
    metadata["canonical_generation_json"] = generation_binding

    coverage = embedding_scheduler.embedding_coverage(
        index_data,
        inventory={"Concept_A": metadata},
        config=config,
    )

    assert coverage["embedded"] == 1
    assert coverage["missing"] == 0
    assert coverage["stale"] == 0
    assert coverage["generation_provenance_drift"] == 1


def test_vector_search_uses_node_input_not_global_generation(
    isolated_memory,
):
    from vector_lake import tool_search
    from vector_lake.search_projection_contract import EMBEDDING_INPUT_CONTRACT

    db_store.init_db()
    index_data = _bind_current_generation(
        {"nodes": {"Concept_A": {"title": "A", "summary": "alpha"}}}
    )
    config, metadata = _current_embedding_metadata(index_data)
    expected = index_data["projection_manifest"]["canonical_generation"][
        "runtime_generations"
    ]
    assert db_store.upsert_embedding_if_current(
        "Concept_A",
        [1.0] * config.dimension,
        content_sha256=metadata["content_sha256"],
        input_contract=EMBEDDING_INPUT_CONTRACT,
        model=config.model,
        dimension=config.dimension,
        expected_runtime_generations=expected,
    ) == "written"

    exact = tool_search._get_vector_search_results(
        [1.0] * config.dimension,
        index_data,
        limit=5,
    )
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE runtime_generations SET generation = generation + 1 "
            "WHERE surface = 'entities'"
        )
    current_index = _bind_current_generation(
        {"nodes": {"Concept_A": {"title": "A", "summary": "alpha"}}}
    )
    generation_drift = tool_search._get_vector_search_results(
        [1.0] * config.dimension,
        current_index,
        limit=5,
    )
    with db_store.transaction():
        db_store.get_connection().execute(
            "UPDATE embedding_metadata_v8 SET canonical_generation_json = '{' "
            "WHERE entity_id = 'Concept_A'"
        )
    malformed_provenance = tool_search._get_vector_search_results(
        [1.0] * config.dimension,
        current_index,
        limit=5,
    )

    assert "Concept_A" in exact
    assert "Concept_A" in generation_drift
    assert "Concept_A" in malformed_provenance


def test_vector_search_expands_knn_past_invalid_nearer_rows(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_search
    from vector_lake.search_projection_contract import EMBEDDING_INPUT_CONTRACT

    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_DIMENSION", "3072")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MODEL", "test-embedding-model")
    db_store.init_db()
    node_keys = (
        "Legacy_Close",
        "Model_Close",
        "Input_Close",
        "Dimension_Close",
        "Content_Close",
        "Valid_Farther",
    )
    index_data = _bind_current_generation(
        {
            "nodes": {
                node_key: {"title": node_key, "summary": f"body {node_key}"}
                for node_key in node_keys
            }
        }
    )
    config = embedding_scheduler.load_embedding_rate_config()
    expected = index_data["projection_manifest"]["canonical_generation"][
        "runtime_generations"
    ]
    close_vector = [1.0, 0.0] + [0.0] * (config.dimension - 2)
    farther_vector = [0.8, 0.6] + [0.0] * (config.dimension - 2)

    db_store.upsert_embedding("Legacy_Close", close_vector)
    for node_key in node_keys[1:]:
        _, metadata = _current_embedding_metadata(index_data, node_key)
        vector = farther_vector if node_key == "Valid_Farther" else close_vector
        assert db_store.upsert_embedding_if_current(
            node_key,
            vector,
            content_sha256=metadata["content_sha256"],
            input_contract=EMBEDDING_INPUT_CONTRACT,
            model=config.model,
            dimension=config.dimension,
            expected_runtime_generations=expected,
        ) == "written"
    provenance_only = {surface: generation + 100 for surface, generation in expected.items()}
    with db_store.transaction():
        conn = db_store.get_connection()
        conn.execute(
            "UPDATE embedding_metadata_v8 SET model = ? WHERE entity_id = ?",
            ("wrong-model", "Model_Close"),
        )
        conn.execute(
            "UPDATE embedding_metadata_v8 SET input_contract = ? WHERE entity_id = ?",
            ("wrong-input", "Input_Close"),
        )
        conn.execute(
            "UPDATE embedding_metadata_v8 SET dimension = ? WHERE entity_id = ?",
            (128, "Dimension_Close"),
        )
        conn.execute(
            "UPDATE embedding_metadata_v8 SET content_sha256 = ? WHERE entity_id = ?",
            ("0" * 64, "Content_Close"),
        )
        conn.execute(
            "UPDATE embedding_metadata_v8 SET canonical_generation_json = ? "
            "WHERE entity_id = ?",
            (json.dumps(provenance_only, sort_keys=True), "Valid_Farther"),
        )

    results = tool_search._get_vector_search_results(
        close_vector,
        index_data,
        limit=1,
    )

    assert list(results) == ["Valid_Farther"]
    assert results["Valid_Farther"] == pytest.approx(0.8, abs=1e-6)


def test_embedding_retry_budget_accepts_explicit_zero(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_RETRIES", "0")

    config = embedding_scheduler.load_embedding_rate_config()

    assert config.max_retries == 0


def test_embedding_backfill_dry_run_counts_missing_vectors(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_RPM", "3000")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_TPM", "1000000")
    db_store.init_db()
    index_data = {
        "nodes": {
            "Concept_A": {"title": "A", "summary": "短文本", "raw_text": "alpha"},
            "Concept_B": {"title": "B", "summary": "另一个短文本", "raw_text": "beta"},
        }
    }
    db_store.upsert_embedding("Concept_A", [1.0] * 3072)

    result = embedding_backfill(index_data, dry_run=True)

    assert result["candidates"] == 2
    assert result["coverage_before"]["embedded"] == 0
    assert result["coverage_before"]["missing"] == 2
    assert result["coverage_before"]["legacy"] == 1
    assert result["effective_rpm"] == 2400
    assert result["effective_tpm"] == 800000


def test_embedding_backfill_dry_run_missing_database_is_zero_write(isolated_memory):
    meta_dir = isolated_memory / "wiki" / ".meta"
    database_path = meta_dir / "vector_lake.db"

    result = embedding_backfill(
        {"nodes": {"Concept_A": {"title": "A", "summary": "alpha"}}},
        dry_run=True,
    )

    assert result["inventory_state"] == "database_missing"
    assert result["candidates"] == 1
    assert "preview assumes no existing vectors" in result["preview_warning"]
    assert not meta_dir.exists()
    assert not database_path.exists()


def test_embedding_backfill_dry_run_reads_existing_database_without_writes(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    db_store.upsert_embedding("Concept_A", [1.0] * 3072)
    database_path = db_store.peek_db_path()
    db_store.close_all_connections()
    before = database_path.stat()
    monkeypatch.setattr(
        db_store,
        "get_connection",
        lambda: (_ for _ in ()).throw(AssertionError("write connection used")),
    )

    result = embedding_backfill(
        {"nodes": {"Concept_A": {"title": "A"}}},
        dry_run=True,
    )
    after = database_path.stat()

    assert result["inventory_state"] == "ready"
    assert result["coverage_before"]["embedded"] == 0
    assert result["coverage_before"]["legacy"] == 1
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_existing_embedding_inventory_reloads_vector_extension_after_close(
    isolated_memory,
):
    db_store.init_db()
    db_store.upsert_embedding("Concept_A", [1.0] * 3072)
    db_store.close_all_connections()

    assert embedding_scheduler.existing_embedding_ids() == {"Concept_A"}


@pytest.mark.parametrize(
    "reader_name",
    ["existing_embedding_ids", "existing_embedding_inventory"],
)
def test_embedding_inventory_preserves_cooperative_cancellation(
    isolated_memory,
    reader_name,
):
    db_store.init_db()
    db_store.upsert_embedding("Concept_A", [1.0] * 3072)
    operation = CancellationOperation(
        tool_name="embedding-inventory-probe",
        lane="read",
        deadline=None,
    )
    operation.mark_running()
    operation.request_cancellation("client_cancelled", detached=True)

    with bind_cancellation_operation(operation):
        with pytest.raises(CooperativeCancellation):
            getattr(embedding_scheduler, reader_name)()


def test_embedding_backfill_fails_closed_when_inventory_is_unavailable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")

    def unavailable_connection():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db_store, "get_connection", unavailable_connection)
    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("provider client must not be created")
        ),
    )

    with pytest.raises(
        embedding_scheduler.EmbeddingInventoryUnavailable,
        match="refusing to treat coverage as empty",
    ):
        embedding_backfill(
            {"nodes": {"Concept_A": {"title": "A"}}},
            dry_run=False,
        )


def test_generate_index_preserves_existing_embeddings_when_compute_returns_empty(isolated_memory, monkeypatch):
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Existing.md", content=_source_content("source_existing", "Existing Source"))
    db_store.upsert_embedding("Source_Existing", [1.0] * 3072)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")
    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: (_ for _ in ()).throw(AssertionError("index rebuild called embedding API")),
    )

    indexer.generate_index()

    conn = db_store.get_connection()
    count = conn.execute("SELECT COUNT(*) FROM vec_embeddings WHERE entity_id = 'Source_Existing'").fetchone()[0]
    assert count == 1


def test_token_estimator_is_conservative_for_cjk_and_latin():
    assert estimate_embedding_tokens("医疗AI agent memory") >= 4


def test_provider_contents_keep_texts_as_separate_requests():
    contents = embedding_scheduler._provider_contents(["first", "second"])

    assert len(contents) == 2
    assert contents[0].parts[0].text == "first"
    assert contents[1].parts[0].text == "second"


def test_embedding_backfill_rejects_partial_provider_response(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_RETRIES", "1")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setattr(embedding_scheduler.time, "sleep", lambda _seconds: None)

    class FakeModels:
        def embed_content(self, **_kwargs):
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] * 3072)])

    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: SimpleNamespace(models=FakeModels()),
    )
    result = embedding_backfill(
        _bind_current_generation({
            "nodes": {
                "Concept_A": {"title": "A"},
                "Concept_B": {"title": "B"},
            }
        }),
        dry_run=False,
    )

    assert result["embedded"] == 0
    assert result["failed_batches"] == 1
    assert "count mismatch" in result["last_error"]
    assert result["stopped"] == "consecutive batch failure guard reached"
    run = db_store.get_connection().execute(
        "SELECT status, processed, failed_batches FROM embedding_runs WHERE run_id = ?",
        (result["run_id"],),
    ).fetchone()
    assert dict(run) == {"status": "failed", "processed": 0, "failed_batches": 1}


def test_embedding_backfill_rejects_wrong_dimension(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_RETRIES", "1")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setattr(embedding_scheduler.time, "sleep", lambda _seconds: None)

    class FakeModels:
        def embed_content(self, **_kwargs):
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] * 8)])

    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: SimpleNamespace(models=FakeModels()),
    )
    result = embedding_backfill(
        _bind_current_generation({"nodes": {"Concept_A": {"title": "A"}}}),
        dry_run=False,
    )

    assert result["embedded"] == 0
    assert "dimension mismatch" in result["last_error"]


def test_embedding_backfill_has_single_writer_lock(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    lock = FileLock(str(isolated_memory / "wiki" / ".meta" / ".embedding-backfill.lock"))
    with lock:
        result = embedding_backfill(
            {"nodes": {"Concept_A": {"title": "A"}}},
            dry_run=False,
        )

    assert result["embedded"] == 0
    assert result["skipped"] == "another embedding backfill is already running"


def test_embedding_backfill_rechecks_inventory_after_writer_lock(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    valid_inventories = iter(
        [
            (set(), {}),
            (set(), {}),
            ({"Concept_A"}, {}),
            ({"Concept_A"}, {}),
        ]
    )
    monkeypatch.setattr(
        embedding_scheduler,
        "existing_embedding_inventory",
        lambda: {},
    )
    monkeypatch.setattr(
        embedding_scheduler,
        "_valid_embedding_ids",
        lambda *_args, **_kwargs: next(valid_inventories),
    )
    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("already-filled node must not call provider")
        ),
    )

    result = embedding_backfill(
        {"nodes": {"Concept_A": {"title": "A"}}},
        dry_run=False,
    )

    assert result["candidates"] == 0
    assert result["embedded"] == 0
    assert result["skipped"] == "no missing embeddings after lock acquisition"
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM embedding_runs"
    ).fetchone()[0] == 0
    lock = FileLock(
        str(isolated_memory / "wiki" / ".meta" / ".embedding-backfill.lock")
    )
    lock.acquire(timeout=0)
    lock.release()


def test_rate_limiter_uses_shared_sqlite_window(isolated_memory, monkeypatch):
    db_store.init_db()
    config = embedding_scheduler.EmbeddingRateConfig(rpm=1, tpm=100, utilization=1.0)
    limiter_a = embedding_scheduler.MinuteRateLimiter(config)
    limiter_b = embedding_scheduler.MinuteRateLimiter(config)
    clock = iter([1000.0, 1000.0, 1060.1])
    sleeps = []
    monkeypatch.setattr(embedding_scheduler.time, "time", lambda: next(clock))
    monkeypatch.setattr(embedding_scheduler.time, "sleep", lambda seconds: sleeps.append(seconds))

    limiter_a.reserve(10)
    limiter_b.reserve(10)

    assert len(sleeps) == 1
    assert sleeps[0] >= 60.0
    rows = db_store.get_connection().execute(
        "SELECT reserved_at, token_count FROM embedding_rate_reservations ORDER BY reserved_at"
    ).fetchall()
    assert [(row["reserved_at"], row["token_count"]) for row in rows] == [(1060.1, 10)]


def test_rebuild_only_reembeds_node_whose_embedding_input_changed(
    isolated_memory,
    monkeypatch,
):
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Changed.md", content=_source_content("source_changed", "Old Title"))
    execute_mutation_plan(
        "Source_Untouched.md",
        content=_source_content("source_untouched", "Untouched Title"),
    )
    indexer.generate_index()
    initial_index = indexer.read_committed_index_snapshot()
    config = embedding_scheduler.load_embedding_rate_config()
    expected = initial_index["projection_manifest"]["canonical_generation"][
        "runtime_generations"
    ]
    from vector_lake.search_projection_contract import EMBEDDING_INPUT_CONTRACT

    for node_key in ("Source_Changed", "Source_Untouched"):
        _, metadata = _current_embedding_metadata(initial_index, node_key)
        assert db_store.upsert_embedding_if_current(
            node_key,
            [1.0] * config.dimension,
            content_sha256=metadata["content_sha256"],
            input_contract=EMBEDDING_INPUT_CONTRACT,
            model=config.model,
            dimension=config.dimension,
            expected_runtime_generations=expected,
        ) == "written"
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")
    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: (_ for _ in ()).throw(AssertionError("incremental index called embedding API")),
    )
    execute_mutation_plan("Source_Changed.md", content=_source_content("source_changed", "New Title"))

    indexer.update_index_items(["Source_Changed.md"])

    current_index = indexer.read_committed_index_snapshot()
    coverage = embedding_scheduler.embedding_coverage(current_index)
    plan = embedding_backfill(current_index, dry_run=True)

    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embeddings WHERE entity_id = 'Source_Changed'"
    ).fetchone()[0] == 0
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embeddings WHERE entity_id = 'Source_Untouched'"
    ).fetchone()[0] == 1
    assert coverage["embedded"] == 1
    assert coverage["missing"] == 1
    assert coverage["stale"] == 0
    assert coverage["generation_provenance_drift"] == 1
    assert plan["candidates"] == 1

    governance_store.save_graph_edges(
        [
            {
                "source_id": "Source_Changed",
                "target_id": "Source_Untouched",
                "relation": "references",
            }
        ]
    )
    indexer.generate_index()
    edge_index = indexer.read_committed_index_snapshot()
    edge_coverage = embedding_scheduler.embedding_coverage(edge_index)
    edge_plan = embedding_backfill(edge_index, dry_run=True)

    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embeddings WHERE entity_id = 'Source_Untouched'"
    ).fetchone()[0] == 1
    assert edge_coverage["embedded"] == 1
    assert edge_coverage["missing"] == 1
    assert edge_coverage["stale"] == 0
    assert edge_coverage["generation_provenance_drift"] == 1
    assert edge_plan["candidates"] == 1


def test_missing_index_fallback_invalidates_touched_vector_without_api(
    isolated_memory,
    monkeypatch,
):
    _purpose(isolated_memory)
    execute_mutation_plan(
        "Source_Changed.md",
        content=_source_content("source_changed", "Changed Title"),
    )
    execute_mutation_plan(
        "Source_Untouched.md",
        content=_source_content("source_untouched", "Untouched Title"),
    )
    db_store.upsert_embedding("Source_Changed", [1.0] * 3072)
    db_store.upsert_embedding("Source_Untouched", [1.0] * 3072)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")
    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("missing-index fallback called embedding API")
        ),
    )

    indexer.update_index_items(["Source_Changed.md"])

    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embeddings WHERE entity_id = 'Source_Changed'"
    ).fetchone()[0] == 0
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embeddings WHERE entity_id = 'Source_Untouched'"
    ).fetchone()[0] == 1


def test_interactive_embed_closes_client_and_disables_retries(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    observed = {}

    class FakeClient:
        closed = False

        def close(self):
            self.closed = True

    client = FakeClient()

    def fake_request(
        request_client,
        contents,
        request_tokens,
        config,
        limiter,
        max_wait_seconds=None,
    ):
        observed.update({
            "client": request_client,
            "contents": contents,
            "tokens": request_tokens,
            "max_retries": config.max_retries,
            "max_wait_seconds": max_wait_seconds,
            "limiter": limiter,
        })
        return [[1.0] * config.dimension]

    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda timeout_ms=None: client,
    )
    monkeypatch.setattr(embedding_scheduler, "_request_embeddings", fake_request)

    values = embedding_scheduler.embed_texts(
        ["interactive query"],
        max_retries=0,
        timeout_ms=1500,
        max_wait_seconds=0.25,
    )

    assert len(values[0]) == 3072
    assert observed["max_retries"] == 0
    assert observed["max_wait_seconds"] == 0.25
    assert client.closed is True


def test_interactive_rate_reservation_fails_fast(isolated_memory):
    db_store.init_db()
    config = embedding_scheduler.EmbeddingRateConfig(
        rpm=1,
        tpm=100,
        utilization=1.0,
    )
    limiter = embedding_scheduler.MinuteRateLimiter(config)
    limiter.reserve(10)

    with pytest.raises(embedding_scheduler.EmbeddingRateLimitTimeout):
        limiter.reserve(10, max_wait_seconds=0.01)


def test_interactive_rate_reservation_can_skip_schema_bootstrap(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    config = embedding_scheduler.EmbeddingRateConfig(
        rpm=10,
        tpm=1_000,
        utilization=1.0,
    )
    monkeypatch.setattr(
        db_store,
        "init_db",
        lambda: (_ for _ in ()).throw(AssertionError("DDL bootstrap in query path")),
    )

    limiter = embedding_scheduler.MinuteRateLimiter(
        config,
        initialize_schema=False,
    )
    limiter.reserve(10, max_wait_seconds=0.25)


def test_start_embedding_run_marks_crashed_run_abandoned(isolated_memory, monkeypatch):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO embedding_runs "
            "(run_id, status, model, candidates, processed, failed_batches, started_at, updated_at) "
            "VALUES ('stale-run', 'running', 'model', 10, 2, 0, '2000-01-01', '2000-01-01')"
        )
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_RUN_STALE_SECONDS", "60")

    db_store.start_embedding_run("new-run", "model", 1)

    rows = {
        row["run_id"]: row["status"]
        for row in conn.execute("SELECT run_id, status FROM embedding_runs")
    }
    assert rows == {"stale-run": "abandoned", "new-run": "running"}


def test_interactive_rate_reservation_deadline_includes_transaction_delay(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    config = embedding_scheduler.EmbeddingRateConfig(
        rpm=1,
        tpm=100,
        utilization=1.0,
    )
    observed = []

    @contextmanager
    def delayed_transaction(max_wait_seconds=None):
        observed.append(max_wait_seconds)
        time.sleep(0.05)
        yield db_store.get_connection()

    monkeypatch.setattr(db_store, "transaction", delayed_transaction)
    limiter = embedding_scheduler.MinuteRateLimiter(config)

    with pytest.raises(embedding_scheduler.EmbeddingRateLimitTimeout):
        limiter.reserve(10, max_wait_seconds=0.01)


def test_embedding_batches_are_lazy_and_bounded():
    config = embedding_scheduler.EmbeddingRateConfig(
        utilization=1.0,
        max_batch_items=2,
        max_batch_tokens=100,
    )
    produced = []

    def items():
        for position in range(5):
            produced.append(position)
            yield {
                "node_key": f"Concept_{position}",
                "text": str(position),
                "tokens": 1,
            }

    batches = embedding_scheduler._batch_items(items(), config)

    assert iter(batches) is batches
    assert produced == []
    first = next(batches)
    assert [item["node_key"] for item in first] == ["Concept_0", "Concept_1"]
    assert produced == [0, 1, 2]
    assert [len(batch) for batch in batches] == [2, 1]


def test_embedding_backfill_processes_one_bounded_batch_at_a_time(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_BATCH_ITEMS", "2")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_RETRIES", "1")
    requested_sizes = []
    written_batch_sizes = []
    progress_updates = []

    class FakeClient:
        closed = False

        def close(self):
            self.closed = True

    client = FakeClient()

    def fake_request(
        _client,
        contents,
        _request_tokens,
        config,
        _limiter,
        max_wait_seconds=None,
    ):
        assert max_wait_seconds is None
        requested_sizes.append(len(contents))
        return [[1.0] * config.dimension for _content in contents]

    monkeypatch.setattr(embedding_scheduler, "_create_client", lambda: client)
    monkeypatch.setattr(embedding_scheduler, "_request_embeddings", fake_request)
    original_batch_upsert = db_store.upsert_embeddings_if_current_batch
    original_progress_update = db_store.update_embedding_run

    def counted_batch_upsert(records, **kwargs):
        written_batch_sizes.append(len(records))
        return original_batch_upsert(records, **kwargs)

    def counted_progress_update(*args, **kwargs):
        progress_updates.append((args, kwargs))
        return original_progress_update(*args, **kwargs)

    monkeypatch.setattr(
        db_store,
        "upsert_embeddings_if_current_batch",
        counted_batch_upsert,
    )
    monkeypatch.setattr(db_store, "update_embedding_run", counted_progress_update)
    index_data = {
        "nodes": {
            f"Concept_{position}": {
                "title": f"Concept {position}",
                "raw_text": "bounded batch",
            }
            for position in range(5)
        }
    }

    result = embedding_backfill(_bind_current_generation(index_data), dry_run=False)

    assert result["candidates"] == 5
    assert result["estimated_requests"] == 3
    assert result["embedded"] == 5
    assert requested_sizes == [2, 2, 1]
    assert written_batch_sizes == [2, 2, 1]
    assert len(progress_updates) == 3
    assert client.closed is True


def test_embedding_provider_return_observes_cancellation_before_batch_cas(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_BATCH_ITEMS", "2")
    operation = CancellationOperation(
        tool_name="embedding_backfill",
        lane="heavy",
        deadline=None,
    )
    operation.mark_running()

    class FakeClient:
        def close(self):
            return None

    def cancel_then_return(
        _client,
        contents,
        _request_tokens,
        config,
        _limiter,
        max_wait_seconds=None,
    ):
        assert max_wait_seconds is None
        operation.request_cancellation("client_cancelled", detached=True)
        return [[1.0] * config.dimension for _content in contents]

    monkeypatch.setattr(embedding_scheduler, "_create_client", FakeClient)
    monkeypatch.setattr(embedding_scheduler, "_request_embeddings", cancel_then_return)
    index_data = _bind_current_generation(
        {
            "nodes": {
                "Concept_A": {"title": "A", "raw_text": "alpha"},
                "Concept_B": {"title": "B", "raw_text": "beta"},
            }
        }
    )

    with bind_cancellation_operation(operation):
        with pytest.raises(CooperativeCancellation):
            embedding_backfill(index_data, dry_run=False)

    assert db_store.count_embeddings() == 0
    assert operation.snapshot()["status"] == "cancelled"


def test_embedding_batch_cas_is_non_interruptible_after_atomic_entry(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_BATCH_ITEMS", "2")
    operation = CancellationOperation(
        tool_name="embedding_backfill",
        lane="heavy",
        deadline=None,
    )
    operation.mark_running()
    entered = threading.Event()
    release = threading.Event()
    result_holder = {}
    errors = []

    class FakeClient:
        def close(self):
            return None

    def fake_request(
        _client,
        contents,
        _request_tokens,
        config,
        _limiter,
        max_wait_seconds=None,
    ):
        assert max_wait_seconds is None
        return [[1.0] * config.dimension for _content in contents]

    original_upsert = db_store.upsert_embeddings_if_current_batch

    def blocking_upsert(records, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_upsert(records, **kwargs)

    monkeypatch.setattr(embedding_scheduler, "_create_client", FakeClient)
    monkeypatch.setattr(embedding_scheduler, "_request_embeddings", fake_request)
    monkeypatch.setattr(
        db_store,
        "upsert_embeddings_if_current_batch",
        blocking_upsert,
    )
    index_data = _bind_current_generation(
        {
            "nodes": {
                "Concept_A": {"title": "A", "raw_text": "alpha"},
                "Concept_B": {"title": "B", "raw_text": "beta"},
            }
        }
    )

    def run_backfill():
        try:
            with bind_cancellation_operation(operation):
                result_holder["result"] = embedding_backfill(
                    index_data,
                    dry_run=False,
                )
                operation.mark_completed()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    worker = threading.Thread(target=run_backfill)
    worker.start()
    assert entered.wait(timeout=5)
    operation.request_cancellation("client_cancelled", detached=True)
    try:
        active = operation.snapshot()
        assert active["status"] == "cancellation_pending"
        assert active["atomic_phase_active"] is True
        assert active["phase"] == "embedding_batch_cas"
    finally:
        release.set()
        worker.join(timeout=10)

    assert worker.is_alive() is False
    assert errors == []
    assert result_holder["result"]["embedded"] == 2
    assert result_holder["result"]["cancellation_pending"] is True
    assert result_holder["result"]["operation_id"] == operation.operation_id
    assert result_holder["result"]["coverage_after"] is None
    assert (
        result_holder["result"]["coverage_after_state"]
        == "not_scanned_due_to_cancellation"
    )
    assert db_store.count_embeddings() == 2
    completed = operation.snapshot()
    assert completed["status"] == "completed_after_cancellation"
    assert completed["detached"] is True


def test_embedding_backfill_counts_generation_drift_as_one_stale_batch(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_BATCH_ITEMS", "2")
    progress_updates = []

    class FakeClient:
        def close(self):
            return None

    def drift_then_return(
        _client,
        contents,
        _request_tokens,
        config,
        _limiter,
        max_wait_seconds=None,
    ):
        assert max_wait_seconds is None
        with db_store.transaction():
            db_store.get_connection().execute(
                "UPDATE runtime_generations SET generation = generation + 1 "
                "WHERE surface = 'entities'"
            )
        return [[1.0] * config.dimension for _content in contents]

    original_progress_update = db_store.update_embedding_run

    def counted_progress_update(*args, **kwargs):
        progress_updates.append((args, kwargs))
        return original_progress_update(*args, **kwargs)

    monkeypatch.setattr(embedding_scheduler, "_create_client", FakeClient)
    monkeypatch.setattr(embedding_scheduler, "_request_embeddings", drift_then_return)
    monkeypatch.setattr(db_store, "update_embedding_run", counted_progress_update)
    index_data = _bind_current_generation(
        {
            "nodes": {
                "Concept_A": {"title": "A", "raw_text": "alpha"},
                "Concept_B": {"title": "B", "raw_text": "beta"},
            }
        }
    )

    result = embedding_backfill(index_data, dry_run=False)

    assert result["embedded"] == 0
    assert result["stale_discarded"] == 2
    assert result["failed_batches"] == 0
    assert len(progress_updates) == 1
    assert db_store.count_embeddings() == 0
    run = db_store.get_connection().execute(
        "SELECT status, processed, failed_batches FROM embedding_runs "
        "WHERE run_id = ?",
        (result["run_id"],),
    ).fetchone()
    assert dict(run) == {"status": "partial", "processed": 0, "failed_batches": 0}


def test_embedding_projection_reuses_shared_index_snapshot(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tool_projection

    index_path = isolated_memory / "wiki" / "index.json"
    index_path.write_text('{"nodes": {}}', encoding="utf-8")
    shared_snapshot = {"nodes": {"Concept_Shared": {"title": "Shared"}}}
    observed = {}

    def fake_load(path):
        observed["path"] = path
        return shared_snapshot

    def fake_backfill(index_data, **kwargs):
        observed["index_data"] = index_data
        observed["kwargs"] = kwargs
        return {
            "dry_run": True,
            "model": "test-model",
            "rpm": 3000,
            "tpm": 1_000_000,
            "utilization": 0.8,
            "effective_rpm": 2400,
            "effective_tpm": 800_000,
            "candidates": 1,
            "estimated_tokens": 1,
            "estimated_requests": 1,
            "coverage_before": {
                "nodes": 1,
                "embedded": 0,
                "missing": 1,
                "stale": 0,
            },
        }

    monkeypatch.setattr(
        tool_projection.indexer,
        "read_committed_index_snapshot",
        fake_load,
    )
    monkeypatch.setattr(embedding_scheduler, "embedding_backfill", fake_backfill)

    output = tool_projection.embedding_backfill_projection(dry_run=True, limit=7)

    assert "[DRY RUN]" in output
    assert observed["path"] == index_path
    assert observed["index_data"] is shared_snapshot
    assert observed["kwargs"] == {
        "dry_run": True,
        "limit": 7,
        "include_existing": False,
    }
