import json
import sqlite3
from types import SimpleNamespace

from vector_lake import (
    db_store,
    embedding_scheduler,
    governance_store,
    indexer,
    runtime_health,
    tool_search,
)
from vector_lake.watchdog_app import process_mutation_outbox_batch


def _base_search_result(payload: str) -> str:
    prefix = "<SemanticReadinessEnvelope>\n"
    suffix = "\n</SemanticReadinessEnvelope>\n"
    assert payload.startswith(prefix)
    envelope_text, result = payload[len(prefix) :].split(suffix, 1)
    assert json.loads(envelope_text)["results_are_not_accepted_facts"] is True
    return result


def _entity(*, raw_text: str = "Stable canonical body.") -> dict:
    return {
        "entity_id": "entity_p1_stability",
        "page_key": "Concept_P1-Stability",
        "canonical_name": "Zygomatic Stability Token",
        "title": "Zygomatic Stability Token",
        "summary": "Committed fallback evidence",
        "raw_text": raw_text,
        "type": "concept",
        "status": "Active",
        "domain": "General",
        "updated": "2026-08-27T00:00:00+00:00",
    }


def test_embedding_response_is_discarded_after_inflight_canonical_change(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    original = _entity()
    governance_store.upsert_entity(original["entity_id"], original)
    indexer.generate_index()
    committed_index = indexer.read_committed_index_snapshot()
    expected_generation = dict(
        committed_index["projection_manifest"]["canonical_generation"][
            "runtime_generations"
        ]
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: SimpleNamespace(close=lambda: None),
    )

    def mutate_while_provider_is_inflight(
        _client,
        contents,
        _request_tokens,
        config,
        _limiter,
        max_wait_seconds=None,
    ):
        assert contents
        assert max_wait_seconds is None
        changed = _entity(raw_text="Canonical content changed during provider call.")
        governance_store.upsert_entity(changed["entity_id"], changed)
        return [[1.0] * config.dimension for _content in contents]

    monkeypatch.setattr(
        embedding_scheduler,
        "_request_embeddings",
        mutate_while_provider_is_inflight,
    )

    result = embedding_scheduler.embedding_backfill(
        committed_index,
        dry_run=False,
    )

    assert result["failed_batches"] == 0, result
    assert indexer.canonical_runtime_generation_snapshot() != expected_generation
    assert result["embedded"] == 0
    assert result["stale_discarded"] == 1
    assert result["coverage_after"]["embedded"] == 0
    assert result["coverage_after"]["missing"] == 1
    conn = db_store.get_vector_connection()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM vec_embeddings WHERE entity_id = ?",
            (original["page_key"],),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM embedding_metadata_v8 WHERE entity_id = ?",
            (original["page_key"],),
        ).fetchone()[0]
        == 0
    )


def test_missing_fts_row_degrades_to_committed_fallback_and_rebuilds(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    entity = _entity()
    governance_store.upsert_entity(entity["entity_id"], entity)
    indexer.generate_index()
    conn = db_store.get_connection()
    state_before = db_store.get_search_projection_state(conn)
    assert state_before["status"] == "ready"
    assert state_before["expected_row_count"] == 1

    with db_store.transaction():
        conn.execute(
            "DELETE FROM wiki_search_index WHERE node_key = ?",
            (entity["page_key"],),
        )

    real_fts_search = tool_search._get_fts_search_results
    fts_calls = []

    def counted_fts_search(*args, **kwargs):
        fts_calls.append(args[0])
        return real_fts_search(*args, **kwargs)

    monkeypatch.setattr(tool_search, "_get_fts_search_results", counted_fts_search)
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda _query: [])
    degraded = tool_search.search_vector_lake("zygomatic stability token", top_k=3)
    degraded_base = _base_search_result(degraded)

    assert degraded_base.startswith("[Search degraded: fts_projection_state]")
    assert "**Zygomatic Stability Token**" in degraded_base
    assert fts_calls == []

    runtime_health._clear_health_caches_for_tests()
    shallow = runtime_health.assess_runtime_health()
    deep = runtime_health.assess_runtime_health(deep_projection_checks=True)
    assert any(
        issue.startswith("fts_projection_row_count_mismatch:")
        for issue in shallow["issues"]
    )
    assert "fts_projection_corpus_mismatch" in deep["issues"]

    indexer.generate_index()
    restored_state = db_store.get_search_projection_state(conn)
    assert restored_state["status"] == "ready"
    assert conn.execute("SELECT COUNT(*) FROM wiki_search_index").fetchone()[0] == 1
    restored = tool_search.search_vector_lake("zygomatic stability token", top_k=3)
    restored_base = _base_search_result(restored)
    assert not restored_base.startswith("[Search degraded:")
    assert "**Zygomatic Stability Token**" in restored_base
    assert len(fts_calls) == 1

    runtime_health._clear_health_caches_for_tests()
    healthy = runtime_health.assess_runtime_health(deep_projection_checks=True)
    assert not any(issue.startswith("fts_projection_") for issue in healthy["issues"])


def test_equal_count_fts_tamper_fails_closed_and_integrity_scan_is_cached(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    entity = _entity()
    governance_store.upsert_entity(entity["entity_id"], entity)
    indexer.generate_index()

    real_inspect = db_store.inspect_search_projection_corpus
    inspections = []

    def counted_inspect(*args, **kwargs):
        inspections.append(1)
        return real_inspect(*args, **kwargs)

    monkeypatch.setattr(db_store, "inspect_search_projection_corpus", counted_inspect)
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda _query: [])

    healthy = tool_search.search_vector_lake("zygomatic stability token", top_k=3)
    assert not _base_search_result(healthy).startswith("[Search degraded:")
    assert len(inspections) == 1
    tool_search.search_vector_lake("zygomatic stability token", top_k=3)
    assert len(inspections) == 1

    conn = db_store.get_connection()
    original_fts_title = str(
        conn.execute(
            "SELECT title FROM wiki_search_index WHERE node_key = ?",
            (entity["page_key"],),
        ).fetchone()[0]
    )
    external = sqlite3.connect(str(db_store.get_db_path()))
    try:
        external.execute(
            "UPDATE wiki_search_index SET title = ? WHERE node_key = ?",
            ("equal count corrupted title", entity["page_key"]),
        )
        external.commit()
    finally:
        external.close()
    state = db_store.get_search_projection_state(conn)
    assert state["status"] == "ready"
    assert state["expected_row_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM wiki_search_index").fetchone()[0] == 1

    degraded = tool_search.search_vector_lake(
        "zygomatic stability token",
        top_k=3,
    )
    degraded_base = _base_search_result(degraded)
    assert degraded_base.startswith("[Search degraded: fts_projection_integrity]")
    assert "**Zygomatic Stability Token**" in degraded_base
    assert len(inspections) == 2

    repeated = tool_search.search_vector_lake(
        "zygomatic stability token",
        top_k=3,
    )
    assert _base_search_result(repeated).startswith(
        "[Search degraded: fts_projection_integrity]"
    )
    assert len(inspections) == 2

    external = sqlite3.connect(str(db_store.get_db_path()))
    try:
        external.execute(
            "UPDATE wiki_search_index SET title = ? WHERE node_key = ?",
            (original_fts_title, entity["page_key"]),
        )
        external.commit()
    finally:
        external.close()

    repaired = tool_search.search_vector_lake(
        "zygomatic stability token",
        top_k=3,
    )
    repaired_base = _base_search_result(repaired)
    assert not repaired_base.startswith("[Search degraded:")
    assert "**Zygomatic Stability Token**" in repaired_base
    assert len(inspections) == 3


def test_equal_count_fts_tamper_during_query_discards_fts_results(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    entity = _entity()
    governance_store.upsert_entity(entity["entity_id"], entity)
    indexer.generate_index()
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda _query: [])
    real_fts_search = tool_search._get_fts_search_results
    fts_calls = []

    def tamper_during_fts(*args, **kwargs):
        fts_calls.append(1)
        conn = db_store.get_connection()
        with db_store.transaction():
            conn.execute(
                "UPDATE wiki_search_index SET title = ? WHERE node_key = ?",
                ("raced corrupted title", entity["page_key"]),
            )
        return real_fts_search(*args, **kwargs)

    monkeypatch.setattr(tool_search, "_get_fts_search_results", tamper_during_fts)

    degraded = tool_search.search_vector_lake(
        "zygomatic stability token",
        top_k=3,
    )
    degraded_base = _base_search_result(degraded)

    assert degraded_base.startswith("[Search degraded: fts_projection_integrity]")
    assert "**Zygomatic Stability Token**" in degraded_base
    assert fts_calls == [1]


def test_fts_integrity_scan_limit_fails_closed_to_committed_fallback(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    entity = _entity()
    governance_store.upsert_entity(entity["entity_id"], entity)
    indexer.generate_index()
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_INTEGRITY_MAX_BYTES", "1")
    monkeypatch.setattr(tool_search, "_get_query_embedding", lambda _query: [])
    monkeypatch.setattr(
        tool_search,
        "_get_fts_search_results",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("FTS must be bypassed when integrity proof is bounded")
        ),
    )

    degraded = tool_search.search_vector_lake(
        "zygomatic stability token",
        top_k=3,
    )
    degraded_base = _base_search_result(degraded)

    assert degraded_base.startswith(
        "[Search degraded: fts_projection_integrity_limit]"
    )
    assert "**Zygomatic Stability Token**" in degraded_base


def test_transient_generation_conflicts_do_not_consume_poison_budget(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    transient_id = db_store.enqueue_mutation(
        "Concept_Transient-Generation.md",
        "delete",
    )
    monkeypatch.setattr(
        indexer,
        "index_projection_matches_canonical",
        lambda _filenames: False,
    )

    def generation_conflict(_filenames):
        raise indexer.ProjectionCanonicalGenerationChanged(
            "canonical generation changed during projection"
        )

    monkeypatch.setattr(indexer, "update_index_items", generation_conflict)

    for _attempt in range(3):
        stats = process_mutation_outbox_batch(
            limit=1,
            max_attempts=3,
            backoff_base=0,
            outbox_ids=[transient_id],
        )
        assert stats == {
            "claimed": 1,
            "completed": 0,
            "retrying": 1,
            "failed": 0,
        }
        db_store.close_all_connections()

    transient = (
        db_store.get_connection()
        .execute(
            "SELECT status, attempt_count, poison_attempt_count, "
            "transient_attempt_count, last_error_code FROM mutation_outbox WHERE id = ?",
            (transient_id,),
        )
        .fetchone()
    )
    assert dict(transient) == {
        "status": "pending",
        "attempt_count": 3,
        "poison_attempt_count": 0,
        "transient_attempt_count": 3,
        "last_error_code": "projection_generation_changed",
    }

    poison_id = db_store.enqueue_mutation(
        "Concept_Deterministic-Payload.md",
        "update",
        payload_text=None,
    )
    observed = []
    for _attempt in range(3):
        stats = process_mutation_outbox_batch(
            limit=1,
            max_attempts=3,
            backoff_base=0,
            outbox_ids=[poison_id],
        )
        observed.append(stats)
        db_store.close_all_connections()

    assert [item["retrying"] for item in observed] == [1, 1, 0]
    assert [item["failed"] for item in observed] == [0, 0, 1]
    poison = (
        db_store.get_connection()
        .execute(
            "SELECT status, attempt_count, poison_attempt_count, "
            "transient_attempt_count, last_error_code, completed_at "
            "FROM mutation_outbox WHERE id = ?",
            (poison_id,),
        )
        .fetchone()
    )
    assert poison["status"] == "failed"
    assert poison["attempt_count"] == 3
    assert poison["poison_attempt_count"] == 3
    assert poison["transient_attempt_count"] == 0
    assert poison["last_error_code"] == "materialization_error"
    assert poison["completed_at"]
    assert (
        process_mutation_outbox_batch(
            limit=1,
            max_attempts=3,
            backoff_base=0,
            outbox_ids=[poison_id],
        )["claimed"]
        == 0
    )
