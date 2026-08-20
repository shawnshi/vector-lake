import time
from contextlib import contextmanager
import pytest

from vector_lake import db_store, indexer
from types import SimpleNamespace

from filelock import FileLock

from vector_lake import embedding_scheduler
from vector_lake.embedding_scheduler import embedding_backfill, estimate_embedding_tokens
from vector_lake.mutation_coordinator import execute_mutation_plan


def _purpose(memory_dir):
    (memory_dir / "purpose.md").write_text(
        """---
purpose_version: "12.0"
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

    assert result["candidates"] == 1
    assert result["coverage_before"]["embedded"] == 1
    assert result["coverage_before"]["missing"] == 1
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
    assert result["coverage_before"]["embedded"] == 1
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_existing_embedding_inventory_reloads_vector_extension_after_close(
    isolated_memory,
):
    db_store.init_db()
    db_store.upsert_embedding("Concept_A", [1.0] * 3072)
    db_store.close_all_connections()

    assert embedding_scheduler.existing_embedding_ids() == {"Concept_A"}


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
        {
            "nodes": {
                "Concept_A": {"title": "A"},
                "Concept_B": {"title": "B"},
            }
        },
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
        {"nodes": {"Concept_A": {"title": "A"}}},
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
    inventories = iter([set(), {"Concept_A"}])
    monkeypatch.setattr(
        embedding_scheduler,
        "existing_embedding_ids",
        lambda: next(inventories),
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


def test_incremental_index_invalidates_stale_vector_without_api(isolated_memory, monkeypatch):
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Changed.md", content=_source_content("source_changed", "Old Title"))
    execute_mutation_plan(
        "Source_Untouched.md",
        content=_source_content("source_untouched", "Untouched Title"),
    )
    indexer.generate_index()
    db_store.upsert_embedding("Source_Changed", [1.0] * 3072)
    db_store.upsert_embedding("Source_Untouched", [1.0] * 3072)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")
    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: (_ for _ in ()).throw(AssertionError("incremental index called embedding API")),
    )
    execute_mutation_plan("Source_Changed.md", content=_source_content("source_changed", "New Title"))

    indexer.update_index_items(["Source_Changed.md"])

    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embeddings WHERE entity_id = 'Source_Changed'"
    ).fetchone()[0] == 0
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embeddings WHERE entity_id = 'Source_Untouched'"
    ).fetchone()[0] == 1


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
    index_data = {
        "nodes": {
            f"Concept_{position}": {
                "title": f"Concept {position}",
                "raw_text": "bounded batch",
            }
            for position in range(5)
        }
    }

    result = embedding_backfill(index_data, dry_run=False)

    assert result["candidates"] == 5
    assert result["estimated_requests"] == 3
    assert result["embedded"] == 5
    assert requested_sizes == [2, 2, 1]
    assert client.closed is True


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
