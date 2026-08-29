import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from vector_lake import db_store, indexer, mcp_server, tool_semantic_campaign
from vector_lake.cancellation import (
    CancellationOperation,
    CooperativeCancellation,
    bind_cancellation_operation,
)
from vector_lake.claim_assessment import record_claim_assessment
from vector_lake.governance_metrics import claim_governance_version
from vector_lake.tool_semantic_campaign import (
    CAMPAIGN_CONTRACT,
    MAX_PAGE_SIZE,
    SemanticCampaignContractError,
    StaleCampaignCursor,
    build_semantic_readiness_campaign_report,
)


@pytest.fixture(autouse=True)
def _clear_campaign_cache():
    tool_semantic_campaign._clear_campaign_snapshot_cache()
    yield
    tool_semantic_campaign._clear_campaign_snapshot_cache()


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _claim(
    claim_id: str,
    *,
    page_key: str,
    evidence_id: str = "",
    source_id: str = "",
    extraction_run_id: str = "",
) -> dict:
    return {
        "claim_id": claim_id,
        "claim_text": f"Claim text for {claim_id}",
        "evidence_ids": [evidence_id] if evidence_id else [],
        "extraction_run_id": extraction_run_id,
        "locator": {"page_key": page_key},
        "source_ids": [source_id] if source_id else [],
        "status": "Active",
    }


def _assessment(claim: dict, *, current: bool) -> dict:
    claim_id = claim["claim_id"]
    return {
        "assessment_id": f"assessment_{claim_id}",
        "assessment_type": "evidence_review",
        "actor_id": "reviewer:test",
        "claim_id": claim_id,
        "claim_version": (
            claim_governance_version(claim) if current else "stale-claim-version"
        ),
        "details": {},
        "method_version": "review-v1",
        "outcome": "supported",
        "reason": "Version-bound test review.",
        "recorded_at": "2026-08-27T00:00:00+00:00",
    }


def _build_campaign_fixture(isolated_memory, monkeypatch) -> dict[str, dict]:
    db_store.init_db()
    connection = db_store.get_connection()
    good_claim = _claim(
        "claim_good",
        page_key="Concept_A",
        evidence_id="evidence_good",
        source_id="source_good",
        extraction_run_id="run_good",
    )
    repairable_claim = _claim(
        "claim_repairable",
        page_key="Concept_B",
        evidence_id="evidence_repairable",
        source_id="source_repairable",
    )
    missing_claim = _claim("claim_missing", page_key="Concept_C")
    claims = {
        claim["claim_id"]: claim
        for claim in (good_claim, repairable_claim, missing_claim)
    }
    entities = {
        "entity_a": {
            "entity_id": "entity_a",
            "canonical_name": "A",
            "links": ["Concept_B"],
            "page_key": "Concept_A",
            "title": "A",
            "type": "concept",
        },
        "entity_b": {
            "entity_id": "entity_b",
            "canonical_name": "B",
            "links": ["Concept_A"],
            "page_key": "Concept_B",
            "title": "B",
            "type": "concept",
        },
        "entity_c": {
            "entity_id": "entity_c",
            "canonical_name": "C",
            "links": [],
            "page_key": "Concept_C",
            "title": "C",
            "type": "concept",
        },
    }
    sources = {
        source_id: {
            "canonical_source_page": f"Source_{source_id}",
            "content_hash": hashlib.sha256(source_id.encode("utf-8")).hexdigest(),
            "integrity_status": "verified",
            "source_id": source_id,
        }
        for source_id in ("source_good", "source_repairable")
    }
    evidence = {
        "evidence_good": {
            "evidence_id": "evidence_good",
            "lineage_safe": True,
            "locator": {"page_key": "Concept_A"},
            "source_id": "source_good",
            "source_locator": {"kind": "text", "paragraph": 1},
        },
        "evidence_repairable": {
            "evidence_id": "evidence_repairable",
            "lineage_safe": True,
            "locator": {"page_key": "Concept_B"},
            "source_id": "source_repairable",
            "source_locator": {"kind": "text", "paragraph": 1},
        },
    }
    assessments = [
        _assessment(good_claim, current=True),
        _assessment(repairable_claim, current=True),
        _assessment(missing_claim, current=False),
    ]
    with db_store.transaction():
        for entity_id, entity in entities.items():
            connection.execute(
                "INSERT INTO entities (entity_id, canonical_name, data_json, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (entity_id, entity["canonical_name"], _json(entity), "2026-08-27T00:00:00+00:00"),
            )
        for claim_id, claim in claims.items():
            connection.execute(
                "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    claim_id,
                    claim["claim_text"],
                    claim["status"],
                    _json(claim),
                    "2026-08-27T00:00:00+00:00",
                ),
            )
        for evidence_id, record in evidence.items():
            connection.execute(
                "INSERT INTO evidence (evidence_id, data_json, updated_at) VALUES (?, ?, ?)",
                (evidence_id, _json(record), "2026-08-27T00:00:00+00:00"),
            )
        for source_id, source in sources.items():
            connection.execute(
                "INSERT INTO sources (source_id, data_json, updated_at) VALUES (?, ?, ?)",
                (source_id, _json(source), "2026-08-27T00:00:00+00:00"),
            )
        identity_owners = [
            ("claim", claim_id, claim["locator"]["page_key"])
            for claim_id, claim in claims.items()
        ]
        identity_owners.extend(
            ("evidence", evidence_id, record["locator"]["page_key"])
            for evidence_id, record in evidence.items()
        )
        for record_kind, record_id, page_key in identity_owners:
            identity = {
                "page_key": page_key,
                "record_id": record_id,
                "record_kind": record_kind,
            }
            connection.execute(
                "INSERT INTO canonical_identities "
                "(record_kind, record_id, page_key, identity_origin, data_json, "
                "recorded_at) VALUES (?, ?, ?, 'canonical_write', ?, ?)",
                (
                    record_kind,
                    record_id,
                    page_key,
                    _json(identity),
                    "2026-08-27T00:00:00+00:00",
                ),
            )
        connection.execute(
            "INSERT INTO extraction_runs "
            "(run_id, page_key, input_fingerprint, extractor_name, extractor_version, "
            "data_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "run_good",
                "Concept_A",
                "sha256:fixture",
                "test",
                "1",
                _json({"run_id": "run_good"}),
                "2026-08-27T00:00:00+00:00",
            ),
        )
        for assessment in assessments:
            connection.execute(
                "INSERT INTO claim_assessments "
                "(assessment_id, claim_id, assessment_type, outcome, actor_id, "
                "method_version, data_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    assessment["assessment_id"],
                    assessment["claim_id"],
                    assessment["assessment_type"],
                    assessment["outcome"],
                    assessment["actor_id"],
                    assessment["method_version"],
                    _json(assessment),
                    assessment["recorded_at"],
                ),
            )

    monkeypatch.setattr(indexer, "_louvain_partition_in_subprocess", lambda *_args: {})
    indexer.generate_index()
    assert indexer.refresh_graph_topology_if_dirty() is True
    db_store.close_all_connections()
    return claims


def _file_state(root) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.stat().st_size)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_campaign_running_scan_cancels_at_next_bounded_checkpoint(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    operation = CancellationOperation(
        tool_name="semantic_readiness_campaign",
        lane="read",
        deadline=None,
    )
    operation.mark_running()
    decoded_claims = []
    original_decode = tool_semantic_campaign._decode_record

    def cancel_after_first_claim(value, *, table, identifier):
        record = original_decode(value, table=table, identifier=identifier)
        if table == "claims":
            decoded_claims.append(identifier)
            if len(decoded_claims) == 1:
                operation.request_cancellation("client_cancelled", detached=True)
        return record

    monkeypatch.setattr(tool_semantic_campaign, "_decode_record", cancel_after_first_claim)
    monkeypatch.setattr(
        tool_semantic_campaign,
        "_CANCELLATION_CHECKPOINT_ROWS",
        1,
        raising=False,
    )

    with bind_cancellation_operation(operation):
        with pytest.raises(CooperativeCancellation):
            build_semantic_readiness_campaign_report(limit=MAX_PAGE_SIZE)

    snapshot = operation.snapshot()
    assert decoded_claims == ["claim_good"]
    assert snapshot["status"] == "cancelled"
    assert snapshot["last_checkpoint"] == "semantic_campaign:claims"
    assert tool_semantic_campaign._campaign_cache_usage() == (0, 0)


def test_campaign_reports_exact_coverage_debt_binding_and_read_only(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    before = _file_state(isolated_memory)

    report = build_semantic_readiness_campaign_report(limit=MAX_PAGE_SIZE)

    assert _file_state(isolated_memory) == before
    assert report["contract"] == CAMPAIGN_CONTRACT
    assert report["read_only"] is True
    assert report["readiness"] == {"ready": False, "status": "not_ready"}
    assert report["coverage"]["evidence"]["numerator"] == 2
    assert report["coverage"]["evidence"]["denominator"] == 3
    assert report["coverage"]["extraction"]["numerator"] == 1
    assert report["coverage"]["extraction"]["denominator"] == 3
    assert report["coverage"]["assessment"]["numerator"] == 2
    assert report["coverage"]["assessment"]["denominator"] == 3
    assert report["coverage"]["source_integrity"]["numerator"] == 2
    assert report["coverage"]["source_integrity"]["denominator"] == 2
    assert report["debt"] == {
        "automatic_repair": 1,
        "human_review": 5,
        "low_evidence_claims": 2,
        "topology_by_type": {
            "fragmented_graph": 1,
            "isolated_node": 1,
            "sparse_community": 1,
        },
        "topology_total": 3,
        "total_findings": 6,
        "unassessed_claims": 1,
        "unique_claims_with_debt": 2,
    }
    binding = report["binding"]
    assert binding["canonical_generation"]["token"].startswith("sha256:")
    shared = binding["projection_graph_generation"]
    assert shared["shared_generation"] is True
    assert shared["projection_generation"] == shared["graph_generation"]
    assert shared["projection_canonical_generation"]["status"] == "verified"
    dispositions = {
        item["debt_id"]: item["disposition"] for item in report["page"]["items"]
    }
    assert dispositions["low-evidence-claim:claim_repairable"] == "automatic_repair"
    assert dispositions["unassessed-claim:claim_missing"] == "human_review"


def test_campaign_cursor_is_stable_bounded_and_rejects_generation_change(
    isolated_memory,
    monkeypatch,
):
    claims = _build_campaign_fixture(isolated_memory, monkeypatch)
    first = build_semantic_readiness_campaign_report(limit=2)
    repeated = build_semantic_readiness_campaign_report(limit=2)

    assert first["page"]["returned"] == 2
    assert first["page"]["next_cursor"]
    assert first["snapshot"]["cache"]["hit"] is False
    assert repeated["page"]["page_fingerprint"] == first["page"]["page_fingerprint"]
    assert repeated["page"]["next_cursor"] == first["page"]["next_cursor"]
    second = build_semantic_readiness_campaign_report(
        limit=2,
        cursor=first["page"]["next_cursor"],
    )
    first_ids = {item["debt_id"] for item in first["page"]["items"]}
    second_ids = {item["debt_id"] for item in second["page"]["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert second["page"]["offset"] == 2
    assert second["campaign_fingerprint"] == first["campaign_fingerprint"]
    assert second["snapshot"]["cache"]["hit"] is True
    assert second["snapshot"]["current_page_rows_scanned"] == 0

    record_claim_assessment(
        "claim_missing",
        assessment_type="evidence_review",
        outcome="needs_review",
        actor_id="reviewer:test",
        method_version="review-v2",
        reason="Current claim version reviewed after page one.",
        expected_claim_version=claim_governance_version(claims["claim_missing"]),
    )
    db_store.close_all_connections()

    def unexpected_rescan():
        pytest.fail("changed cursor must become stale without a full snapshot rebuild")

    monkeypatch.setattr(
        tool_semantic_campaign,
        "_build_campaign_snapshot",
        unexpected_rescan,
    )
    with pytest.raises(StaleCampaignCursor, match="restart pagination"):
        build_semantic_readiness_campaign_report(
            limit=2,
            cursor=first["page"]["next_cursor"],
        )


def test_campaign_cursor_stays_bound_when_same_semantics_get_a_new_source_fingerprint(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    first = build_semantic_readiness_campaign_report(limit=1)
    old_cursor = first["page"]["next_cursor"]
    assert old_cursor

    db_store.start_embedding_run("source-fingerprint-noise", "test-model", 0)
    db_store.close_all_connections()
    current = build_semantic_readiness_campaign_report(limit=1)

    assert current["debt_inventory_fingerprint"] == first[
        "debt_inventory_fingerprint"
    ]
    assert current["campaign_fingerprint"] != first["campaign_fingerprint"]

    def unexpected_rescan():
        pytest.fail("stale cursor must not rebuild after a source fingerprint change")

    monkeypatch.setattr(
        tool_semantic_campaign,
        "_build_campaign_snapshot",
        unexpected_rescan,
    )
    with pytest.raises(StaleCampaignCursor, match="changed"):
        build_semantic_readiness_campaign_report(limit=1, cursor=old_cursor)


def test_campaign_cursor_page_reuses_one_counted_bounded_snapshot(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    build_calls = 0
    original_build = tool_semantic_campaign._build_campaign_snapshot

    def counted_build():
        nonlocal build_calls
        build_calls += 1
        return original_build()

    monkeypatch.setattr(
        tool_semantic_campaign,
        "_build_campaign_snapshot",
        counted_build,
    )
    first = build_semantic_readiness_campaign_report(limit=1)
    before_cached_page = _file_state(isolated_memory)
    second = build_semantic_readiness_campaign_report(
        limit=1,
        cursor=first["page"]["next_cursor"],
    )

    assert build_calls == 1
    assert _file_state(isolated_memory) == before_cached_page
    assert first["snapshot"]["initial_scan"]["rows_by_table"] == {
        "claim_assessments": 3,
        "claims": 3,
        "evidence": 2,
        "extraction_runs": 1,
        "runtime_generations": 7,
        "sources": 2,
    }
    assert first["snapshot"]["initial_scan"]["rows_total"] == 18
    assert first["snapshot"]["initial_scan"]["json_bytes"] > 0
    assert (
        first["snapshot"]["initial_scan"]["source_bytes"]
        > first["snapshot"]["initial_scan"]["json_bytes"]
    )
    assert first["snapshot"]["initial_scan"]["graph_materialization_bytes"] > 0
    assert (
        first["snapshot"]["initial_scan"]["debt_materialization_bytes"]
        == first["snapshot"]["initial_scan"]["cached_inventory_bytes"]
    )
    assert first["snapshot"]["initial_scan"]["cached_inventory_bytes"] > 0
    assert second["snapshot"]["initial_scan"] == first["snapshot"]["initial_scan"]
    assert second["snapshot"]["current_page_rows_scanned"] == 0


def test_campaign_repeated_first_page_reuses_matching_source_snapshot(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    build_calls = 0
    original_build = tool_semantic_campaign._build_campaign_snapshot

    def counted_build():
        nonlocal build_calls
        build_calls += 1
        return original_build()

    monkeypatch.setattr(tool_semantic_campaign, "_build_campaign_snapshot", counted_build)

    first = build_semantic_readiness_campaign_report(limit=2)
    repeated = build_semantic_readiness_campaign_report(limit=2)

    assert build_calls == 1
    assert repeated["campaign_fingerprint"] == first["campaign_fingerprint"]
    assert repeated["page"] == first["page"]
    assert first["snapshot"]["current_page_rows_scanned"] == 18
    assert repeated["snapshot"]["cache"]["hit"] is True
    assert repeated["snapshot"]["current_page_rows_scanned"] == 0


def test_campaign_concurrent_first_pages_single_flight_by_generation(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    template = tool_semantic_campaign._build_campaign_snapshot()
    tool_semantic_campaign._clear_campaign_snapshot_cache()
    build_calls = 0
    build_lock = threading.Lock()
    release_build = threading.Event()
    start_barrier = threading.Barrier(4)

    def slow_build():
        nonlocal build_calls
        with build_lock:
            build_calls += 1
        assert release_build.wait(timeout=5)
        return template

    def first_page():
        start_barrier.wait(timeout=5)
        return build_semantic_readiness_campaign_report(limit=2)

    monkeypatch.setattr(tool_semantic_campaign, "_build_campaign_snapshot", slow_build)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(first_page) for _ in range(4)]
        time.sleep(0.2)
        release_build.set()
        reports = [future.result(timeout=10) for future in futures]

    assert build_calls == 1
    assert {report["campaign_fingerprint"] for report in reports} == {
        template.campaign_fingerprint
    }
    assert sum(report["snapshot"]["cache"]["hit"] is False for report in reports) == 1


def test_campaign_initial_snapshot_row_limit_fails_closed(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    limits = dict(tool_semantic_campaign.SNAPSHOT_ROW_LIMITS)
    limits["claims"] = 2
    monkeypatch.setattr(tool_semantic_campaign, "SNAPSHOT_ROW_LIMITS", limits)

    with pytest.raises(
        SemanticCampaignContractError,
        match="snapshot row limit exceeded: claims > 2",
    ):
        build_semantic_readiness_campaign_report(limit=1)
    assert len(tool_semantic_campaign._CAMPAIGN_CACHE) == 0


def test_campaign_preflights_long_text_in_total_bytes_before_row_processing(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    baseline = build_semantic_readiness_campaign_report(limit=1)
    scan = baseline["snapshot"]["initial_scan"]
    pre_assessment_tables = ("claims", "evidence", "sources", "extraction_runs")
    pre_assessment_bytes = sum(
        scan["bytes_by_table"][table] for table in pre_assessment_tables
    )
    tool_semantic_campaign._clear_campaign_snapshot_cache()

    long_recorded_at = "界" * 1024
    connection = db_store.get_connection()
    with db_store.transaction():
        connection.execute(
            "UPDATE extraction_runs SET recorded_at = ? WHERE run_id = ?",
            (long_recorded_at, "run_good"),
        )
    db_store.close_all_connections()
    monkeypatch.setattr(
        tool_semantic_campaign,
        "MAX_SNAPSHOT_SOURCE_BYTES",
        pre_assessment_bytes,
    )
    monkeypatch.setattr(
        tool_semantic_campaign,
        "MAX_SNAPSHOT_TEXT_FIELD_BYTES",
        len(long_recorded_at.encode("utf-8")) + 1,
    )

    def unexpected_row_processing(*_args, **_kwargs):
        pytest.fail("oversized TEXT row was processed before aggregate byte preflight")

    monkeypatch.setattr(
        tool_semantic_campaign,
        "_update_ordered_row_digest",
        unexpected_row_processing,
    )
    with pytest.raises(
        SemanticCampaignContractError,
        match="snapshot source byte limit exceeded",
    ):
        build_semantic_readiness_campaign_report(limit=1)
    assert len(tool_semantic_campaign._CAMPAIGN_CACHE) == 0


def test_campaign_preflights_oversized_graph_bytes_before_projection_decode(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    projection_state = tool_semantic_campaign._projection_snapshot_identity()
    materialization_bytes = projection_state["materialization_bytes"]
    assert materialization_bytes > 0
    monkeypatch.setattr(
        tool_semantic_campaign,
        "MAX_GRAPH_MATERIALIZATION_BYTES",
        materialization_bytes - 1,
    )

    def unexpected_projection_decode(*_args, **_kwargs):
        pytest.fail("oversized graph was decoded before artifact byte preflight")

    monkeypatch.setattr(
        indexer,
        "read_committed_index_snapshot",
        unexpected_projection_decode,
    )
    with pytest.raises(
        SemanticCampaignContractError,
        match="graph materialization byte limit exceeded before decode",
    ):
        build_semantic_readiness_campaign_report(limit=1)
    assert len(tool_semantic_campaign._CAMPAIGN_CACHE) == 0


def test_debt_materialization_byte_limit_rejects_before_inventory_accumulation(
    monkeypatch,
):
    budget = tool_semantic_campaign._DebtBudget()
    items = []
    item = {
        "debt_id": "oversized-debt:" + ("界" * 1024),
        "debt_type": "isolated_node",
        "disposition": "human_review",
        "reasons": ["bounded-test"],
        "remediation": {"automatic_apply": False, "tool": "review"},
        "subject_id": "node:test",
    }
    item_bytes = len(tool_semantic_campaign._canonical_json_bytes(item))
    monkeypatch.setattr(
        tool_semantic_campaign,
        "MAX_DEBT_ITEM_BYTES",
        item_bytes,
    )
    monkeypatch.setattr(
        tool_semantic_campaign,
        "MAX_DEBT_MATERIALIZATION_BYTES",
        item_bytes + 1,
    )

    with pytest.raises(
        SemanticCampaignContractError,
        match="debt materialization byte limit exceeded",
    ):
        budget.append(items, item)
    assert items == []
    assert budget.count == 0
    assert budget.materialized_bytes == 2


def test_campaign_cursor_cache_expiry_is_stale_without_rescan(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(
        tool_semantic_campaign,
        "_campaign_cache_now",
        lambda: clock[0],
    )
    first = build_semantic_readiness_campaign_report(limit=1)
    cursor = first["page"]["next_cursor"]
    assert cursor
    clock[0] += tool_semantic_campaign.CAMPAIGN_CACHE_TTL_SECONDS + 0.001

    def unexpected_rescan():
        pytest.fail("expired cursor must not rebuild the full snapshot")

    monkeypatch.setattr(
        tool_semantic_campaign,
        "_build_campaign_snapshot",
        unexpected_rescan,
    )
    with pytest.raises(StaleCampaignCursor, match="cache expired"):
        build_semantic_readiness_campaign_report(limit=1, cursor=cursor)
    assert tool_semantic_campaign._CAMPAIGN_CACHE_BYTES == 0


def test_campaign_cursor_uses_configurable_sliding_lease_for_full_traversal(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    clock = [100.0]
    monkeypatch.setenv(
        "VECTOR_LAKE_SEMANTIC_CAMPAIGN_CURSOR_TTL_SECONDS",
        "2",
    )
    monkeypatch.setattr(
        tool_semantic_campaign,
        "_campaign_cache_now",
        lambda: clock[0],
    )

    first = build_semantic_readiness_campaign_report(limit=1)
    assert first["snapshot"]["cache"]["ttl_seconds"] == 2.0
    hard_limits = first["snapshot"]["hard_limits"]
    assert hard_limits["max_pages_at_max_page_size"] == (
        hard_limits["debt_items"] + hard_limits["findings_per_page"] - 1
    ) // hard_limits["findings_per_page"]

    clock[0] += 1.5
    second = build_semantic_readiness_campaign_report(
        limit=1,
        cursor=first["page"]["next_cursor"],
    )
    clock[0] += 1.5
    third = build_semantic_readiness_campaign_report(
        limit=1,
        cursor=second["page"]["next_cursor"],
    )
    assert third["page"]["offset"] == 2

    clock[0] += 2.001

    def unexpected_rescan():
        pytest.fail("expired cursor cache miss must not rebuild the snapshot")

    monkeypatch.setattr(
        tool_semantic_campaign,
        "_build_campaign_snapshot",
        unexpected_rescan,
    )
    with pytest.raises(StaleCampaignCursor, match="cache expired"):
        build_semantic_readiness_campaign_report(
            limit=1,
            cursor=third["page"]["next_cursor"],
        )


def test_campaign_cache_enforces_global_lru_byte_budget(
    isolated_memory,
    monkeypatch,
):
    claims = _build_campaign_fixture(isolated_memory, monkeypatch)
    baseline = build_semantic_readiness_campaign_report(limit=1)
    entry_bytes = baseline["snapshot"]["cache"]["entry_bytes"]
    tool_semantic_campaign._clear_campaign_snapshot_cache()
    monkeypatch.setattr(tool_semantic_campaign, "MAX_CAMPAIGN_CACHE_ENTRIES", 10)
    monkeypatch.setattr(
        tool_semantic_campaign,
        "MAX_CAMPAIGN_CACHE_BYTES",
        entry_bytes + 4096,
    )
    first = build_semantic_readiness_campaign_report(limit=1)
    old_cursor = first["page"]["next_cursor"]
    assert old_cursor

    record_claim_assessment(
        "claim_missing",
        assessment_type="evidence_review",
        outcome="needs_review",
        actor_id="reviewer:test",
        method_version="review-byte-budget",
        reason="Force a second source-bound semantic snapshot.",
        expected_claim_version=claim_governance_version(claims["claim_missing"]),
    )
    db_store.close_all_connections()
    current = build_semantic_readiness_campaign_report(limit=1)

    cache = current["snapshot"]["cache"]
    assert cache["total_bytes"] <= cache["max_bytes"]
    assert len(tool_semantic_campaign._CAMPAIGN_CACHE) == 1
    with pytest.raises(StaleCampaignCursor, match="cache is unavailable"):
        build_semantic_readiness_campaign_report(limit=1, cursor=old_cursor)


def test_campaign_cache_rejects_single_entry_over_global_byte_budget(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    baseline = build_semantic_readiness_campaign_report(limit=1)
    entry_bytes = baseline["snapshot"]["cache"]["entry_bytes"]
    tool_semantic_campaign._clear_campaign_snapshot_cache()
    monkeypatch.setattr(
        tool_semantic_campaign,
        "MAX_CAMPAIGN_CACHE_BYTES",
        entry_bytes - 1,
    )

    with pytest.raises(
        SemanticCampaignContractError,
        match="cache entry byte limit exceeded",
    ):
        build_semantic_readiness_campaign_report(limit=1)
    assert len(tool_semantic_campaign._CAMPAIGN_CACHE) == 0
    assert tool_semantic_campaign._CAMPAIGN_CACHE_BYTES == 0


def test_campaign_cursor_cache_is_lru_bounded(
    isolated_memory,
    monkeypatch,
):
    claims = _build_campaign_fixture(isolated_memory, monkeypatch)
    monkeypatch.setattr(tool_semantic_campaign, "MAX_CAMPAIGN_CACHE_ENTRIES", 1)
    first = build_semantic_readiness_campaign_report(limit=1)
    old_cursor = first["page"]["next_cursor"]
    assert old_cursor

    record_claim_assessment(
        "claim_missing",
        assessment_type="evidence_review",
        outcome="needs_review",
        actor_id="reviewer:test",
        method_version="review-v3",
        reason="Create a second bounded campaign snapshot.",
        expected_claim_version=claim_governance_version(claims["claim_missing"]),
    )
    db_store.close_all_connections()
    current = build_semantic_readiness_campaign_report(limit=1)

    assert len(tool_semantic_campaign._CAMPAIGN_CACHE) == 1
    assert current["campaign_fingerprint"] in tool_semantic_campaign._CAMPAIGN_CACHE
    with pytest.raises(StaleCampaignCursor, match="cache is unavailable"):
        build_semantic_readiness_campaign_report(limit=1, cursor=old_cursor)


def test_campaign_rejects_unbounded_or_tampered_page_requests(
    isolated_memory,
    monkeypatch,
):
    _build_campaign_fixture(isolated_memory, monkeypatch)
    with pytest.raises(ValueError, match="between 1"):
        build_semantic_readiness_campaign_report(limit=MAX_PAGE_SIZE + 1)
    first = build_semantic_readiness_campaign_report(limit=1)
    cursor = first["page"]["next_cursor"]
    assert cursor
    replacement = "A" if cursor[-1] != "A" else "B"
    with pytest.raises(ValueError, match="cursor"):
        build_semantic_readiness_campaign_report(
            limit=1,
            cursor=cursor[:-1] + replacement,
        )


def test_campaign_missing_database_fails_closed_without_initializing(isolated_memory):
    meta_dir = isolated_memory / "wiki" / ".meta"
    assert meta_dir.exists() is False

    with pytest.raises(db_store.ReadOnlySnapshotUnavailable, match="database_missing"):
        build_semantic_readiness_campaign_report()

    assert meta_dir.exists() is False


def test_campaign_is_registered_as_a_bounded_read_only_scan():
    from vector_lake import tool_registry

    assert callable(mcp_server.semantic_readiness_campaign)
    assert "semantic_readiness_campaign_report" in tool_registry.__all__
    assert callable(tool_registry.semantic_readiness_campaign_report)
    assert mcp_server._MCP_HEAVY_TASKS["semantic_readiness_campaign"] == (
        "scan",
        900.0,
    )
