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
categories: [Source]
updated: 2026-07-13T00:00:00+00:00
sources: [raw/test.pdf]
strategic_scope: core
evidence_tier: primary
---
Primary source content.
"""


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
    indexer.generate_index()
    db_store.upsert_embedding("Source_Changed", [1.0] * 3072)
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


# --- caller-supplied budget -------------------------------------------------
#
# A quota error used to sleep a flat 60 s per retry, which exceeds the MCP
# client's 60 s call ceiling on its own.  These pin the deadline check that turns
# an unaffordable wait into an explicit, immediate failure.


class _StubLimiter:
    def __init__(self, wait_seconds=0.0):
        self.wait_seconds = wait_seconds
        self.calls = 0
        self.deadlines = []
        self.durable = []

    def reserve(self, request_tokens, deadline=None, durable=True):
        self.calls += 1
        self.deadlines.append(deadline)
        self.durable.append(durable)
        if self.wait_seconds:
            raise embedding_scheduler.EmbeddingBudgetExceeded(
                f"rate-limit window would hold this request for {self.wait_seconds}s"
            )


class _QuotaClient:
    """Raises the provider's quota error on every call."""

    def __init__(self):
        self.models = SimpleNamespace(embed_content=self._embed)
        self.calls = 0

    def _embed(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")


def test_quota_retry_inside_a_budget_fails_fast(monkeypatch):
    slept = []
    monkeypatch.setattr(embedding_scheduler.time, "sleep", lambda seconds: slept.append(seconds))
    client = _QuotaClient()

    with pytest.raises(embedding_scheduler.EmbeddingBudgetExceeded) as caught:
        embedding_scheduler._request_embeddings(
            client,
            ["梅奥"],
            10,
            embedding_scheduler.load_embedding_rate_config(),
            _StubLimiter(),
            budget_seconds=1.0,
        )

    assert slept == []  # the 60 s quota sleep was refused, not taken
    assert client.calls >= 1
    assert "budget remains" in str(caught.value)


def test_quota_retry_will_wait_when_no_budget_was_given(monkeypatch):
    """Batch callers (backfill) must keep the patient behaviour."""
    slept = []
    monkeypatch.setattr(embedding_scheduler.time, "sleep", lambda seconds: slept.append(seconds))
    config = embedding_scheduler.load_embedding_rate_config()

    with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
        embedding_scheduler._request_embeddings(
            _QuotaClient(), ["梅奥"], 10, config, _StubLimiter(), budget_seconds=None
        )

    assert slept == [60.0] * config.max_retries


def test_rate_limiter_refuses_an_unaffordable_window_wait(isolated_memory, monkeypatch):
    """A full rolling window must not be waited out past the caller's deadline."""
    db_store.init_db()
    monkeypatch.setattr(
        embedding_scheduler.time, "sleep", lambda seconds: pytest.fail("slept past the budget")
    )
    config = embedding_scheduler.load_embedding_rate_config()
    limiter = embedding_scheduler.MinuteRateLimiter(config)
    now = embedding_scheduler.time.time()
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(config.effective_rpm):
            conn.execute(
                "INSERT INTO embedding_rate_reservations (reservation_id, reserved_at, token_count) "
                "VALUES (?, ?, ?)",
                (f"res_{index}", now, 1),
            )

    with pytest.raises(embedding_scheduler.EmbeddingBudgetExceeded):
        limiter.reserve(1, deadline=embedding_scheduler.time.monotonic())


def test_rate_limiter_still_blocks_without_a_deadline(isolated_memory, monkeypatch):
    """No deadline means the limiter waits, as the batch paths require."""
    db_store.init_db()
    slept = []
    config = embedding_scheduler.load_embedding_rate_config()
    limiter = embedding_scheduler.MinuteRateLimiter(config)
    now = embedding_scheduler.time.time()
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(config.effective_rpm):
            conn.execute(
                "INSERT INTO embedding_rate_reservations (reservation_id, reserved_at, token_count) "
                "VALUES (?, ?, ?)",
                (f"res_{index}", now, 1),
            )

    def _stop_after_one_wait(seconds):
        slept.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(embedding_scheduler.time, "sleep", _stop_after_one_wait)
    with pytest.raises(KeyboardInterrupt):
        limiter.reserve(1)
    assert slept and slept[0] > 59.0


def test_interactive_reservation_does_not_take_the_write_lock(isolated_memory, monkeypatch):
    """A query embedding must not put the write lock back on the read path."""
    db_store.init_db()
    config = embedding_scheduler.load_embedding_rate_config()
    limiter = embedding_scheduler.MinuteRateLimiter(config)

    def _forbidden(*_a, **_k):
        raise AssertionError("an interactive reservation attempted the write lock")

    monkeypatch.setattr(db_store, "_acquire_write_lock", _forbidden)

    limiter.reserve(10, durable=False)  # must not raise

    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM embedding_rate_reservations").fetchone()[0] == 0


def test_batch_reservation_still_records_durably(isolated_memory):
    db_store.init_db()
    config = embedding_scheduler.load_embedding_rate_config()
    limiter = embedding_scheduler.MinuteRateLimiter(config)

    limiter.reserve(10)
    limiter.reserve(10, durable=False)

    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM embedding_rate_reservations").fetchone()[0] == 1


def test_interactive_reservation_still_refuses_a_saturated_window(isolated_memory, monkeypatch):
    """Read-only does not mean unguarded: a full durable window still degrades."""
    db_store.init_db()
    config = embedding_scheduler.load_embedding_rate_config()
    limiter = embedding_scheduler.MinuteRateLimiter(config)
    now = embedding_scheduler.time.time()
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(config.effective_rpm):
            conn.execute(
                "INSERT INTO embedding_rate_reservations (reservation_id, reserved_at, token_count) "
                "VALUES (?, ?, ?)",
                (f"res_{index}", now, 1),
            )
    monkeypatch.setattr(
        embedding_scheduler.time, "sleep", lambda seconds: pytest.fail("slept past the budget")
    )

    with pytest.raises(embedding_scheduler.EmbeddingBudgetExceeded):
        limiter.reserve(1, deadline=embedding_scheduler.time.monotonic(), durable=False)
