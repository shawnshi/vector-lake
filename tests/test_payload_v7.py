import base64
import copy
import hashlib
import inspect
import json
import random
import sqlite3
import zlib
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store


V6_CHECKSUM = "e7f0edcf4060bc6ddee68f46506d583c3c55428686f685a19affd5c414cb52fe"
V8_CHECKSUM = "0bfa98f0e74063aed8c8cae28028bd48426b5f532e917f6e00f50c879c7d3278"


def _downgrade_search_runtime_contract_to_v7(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS projection_runtime_v9")
    connection.execute("DROP TABLE IF EXISTS embedding_metadata_v8")
    connection.execute("DROP TABLE IF EXISTS search_projection_state_v8")
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(mutation_outbox)").fetchall()
    }
    for column_name in (
        "poison_attempt_count",
        "transient_attempt_count",
        "last_error_code",
        "first_transient_at",
    ):
        if column_name in columns:
            connection.execute(
                f'ALTER TABLE mutation_outbox DROP COLUMN "{column_name}"'
            )


def _change_set(
    change_set_id: str,
    *,
    idempotency_key: str | None = None,
    page: str = "Concept_A.md",
    created_at: str = "2026-08-03T00:00:00+00:00",
    payload_tag: str = "a",
) -> dict:
    return {
        "change_set_id": change_set_id,
        "idempotency_key": idempotency_key or f"idem-{change_set_id}",
        "origin": "payload-v7-candidate-test",
        "created_at": created_at,
        "status": "pending",
        "summary": "payload v7 boundary test",
        "requires_human_review": True,
        "affected_ids": [f"entity-{payload_tag}", f"claim-{payload_tag}"],
        "affected_pages": [page],
        "proposed_entities": [
            {
                "entity_id": f"entity-{payload_tag}",
                "canonical_name": "A",
                "page_key": "Concept_A",
            }
        ],
        "proposed_claims": [
            {
                "claim_id": f"claim-{payload_tag}",
                "claim_family_id": f"family-{payload_tag}",
                "claim_text": "",
                "locator": {"page_key": "Concept_A"},
            }
        ],
        "proposed_evidence": [],
        "proposed_source_updates": [],
        "proposed_source_artifacts": [],
        "proposed_extraction_runs": [],
        "proposed_edges": [],
        "write_contract": {"transactional": True},
    }


def _with_exact_payload_size(change_set: dict, target_bytes: int) -> tuple[dict, bytes]:
    sized = copy.deepcopy(change_set)
    sized["proposed_claims"][0]["claim_text"] = ""
    _payload, baseline = governance_store._canonical_change_set_payload(sized)
    padding_bytes = target_bytes - len(baseline)
    if padding_bytes < 0:
        raise AssertionError("target payload is smaller than the canonical baseline")
    sized["proposed_claims"][0]["claim_text"] = "x" * padding_bytes
    _payload, raw = governance_store._canonical_change_set_payload(sized)
    assert len(raw) == target_bytes
    return sized, raw


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "change_sets",
            "change_set_payloads",
            "change_set_payload_refs",
            "change_set_lifecycle_v6",
            "change_set_idempotency",
        )
    }


def _payload_snapshots(path: Path) -> tuple[list[tuple], list[tuple]]:
    connection = sqlite3.connect(path)
    try:
        payloads = connection.execute(
            "SELECT payload_sha256, codec, payload_blob, raw_bytes, stored_bytes, "
            "created_at FROM change_set_payloads ORDER BY payload_sha256"
        ).fetchall()
        refs = connection.execute(
            "SELECT change_set_id, payload_sha256, created_at "
            "FROM change_set_payload_refs ORDER BY change_set_id"
        ).fetchall()
        return payloads, refs
    finally:
        connection.close()


def _downgrade_payload_schema_to_v6(path: Path) -> tuple[list[tuple], list[tuple]]:
    db_store.close_all_connections()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        payloads, refs = _payload_snapshots(path)
        connection.execute("BEGIN IMMEDIATE")
        _downgrade_search_runtime_contract_to_v7(connection)
        connection.execute("DROP TABLE change_set_payload_refs")
        connection.execute("DROP TABLE change_set_payloads")
        connection.execute(db_store._CHANGE_SET_PAYLOADS_TABLE_SCHEMA_V6)
        connection.execute(db_store._CHANGE_SET_PAYLOAD_REFS_TABLE_SCHEMA_V6)
        connection.execute(db_store._CHANGE_SET_PAYLOAD_REFS_INDEX_SCHEMA_V6)
        connection.executemany(
            "INSERT INTO change_set_payloads "
            "(payload_sha256, codec, payload_blob, raw_bytes, stored_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            payloads,
        )
        connection.executemany(
            "INSERT INTO change_set_payload_refs "
            "(change_set_id, payload_sha256, created_at) VALUES (?, ?, ?)",
            refs,
        )
        connection.execute("DELETE FROM schema_migrations WHERE version > 6")
        connection.execute("PRAGMA user_version = 6")
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))
    return payloads, refs


def _physical_v6_with_shared_pending_payload() -> tuple[Path, list[tuple], list[tuple]]:
    db_store.init_db()
    first = _change_set("v6-pending-a", idempotency_key="v6-idem-a")
    second = _change_set("v6-pending-b", idempotency_key="v6-idem-b")
    assert governance_store.record_prepared_change_sets([first]) == 1
    assert governance_store.record_prepared_change_sets([second]) == 1
    path = db_store.get_db_path().resolve()
    payloads, refs = _downgrade_payload_schema_to_v6(path)
    assert len(payloads) == 1
    assert len(refs) == 2
    state = db_store.inspect_schema_migration_state(path)
    assert state["user_version"] == 6
    assert state["issues"] == []
    return path, payloads, refs


def _v6_database_snapshot(path: Path) -> dict:
    connection = sqlite3.connect(path)
    try:
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "ledger": connection.execute(
                "SELECT version, name, checksum, applied_at "
                "FROM schema_migrations ORDER BY version"
            ).fetchall(),
            "schema": connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name IN ('change_set_payloads', 'change_set_payload_refs', "
                "'idx_change_set_payload_refs_payload_v6') ORDER BY type, name"
            ).fetchall(),
            "payloads": _payload_snapshots(path)[0],
            "refs": _payload_snapshots(path)[1],
            "foreign_keys": connection.execute(
                "PRAGMA foreign_key_list(change_set_payload_refs)"
            ).fetchall(),
        }
    finally:
        connection.close()


def test_fresh_current_schema_preserves_v7_v8_and_adds_exact_v9_contract(
    isolated_memory,
):
    db_store.init_db()
    connection = db_store.get_connection()
    ledger = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()

    assert db_store._SCHEMA_VERSION == 9
    assert db_store._SCHEMA_MIGRATION_SUPPORTED_SOURCE_VERSIONS == {
        4,
        5,
        6,
        7,
        8,
        9,
    }
    assert [row[0] for row in ledger] == list(range(1, 10))
    assert ledger[5][1:] == ("change_set_delta_history_v6", V6_CHECKSUM)
    assert db_store._SCHEMA_MIGRATIONS[6][1] == V6_CHECKSUM
    assert ledger[7][1:] == ("search_projection_integrity_v8", V8_CHECKSUM)
    assert db_store._SCHEMA_MIGRATIONS[8][1] == V8_CHECKSUM
    assert governance_store._CHANGE_SET_MAX_PAYLOAD_BYTES == 8 * 1024 * 1024
    assert governance_store._CHANGE_SET_MAX_STORED_BYTES == 4 * 1024 * 1024 + 64 * 1024
    assert governance_store._CHANGE_SET_BATCH_MAX_PAYLOAD_BYTES == 32 * 1024 * 1024
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
    assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db_store._change_set_payload_schema_v7_issues(connection) == []
    assert db_store._search_runtime_schema_v8_issues(connection) == []
    assert db_store._projection_runtime_schema_v9_issues(connection) == []
    for (
        object_type,
        name,
        expected_sql,
    ) in db_store._CHANGE_SET_PAYLOAD_SCHEMA_OBJECTS_V7:
        observed = connection.execute(
            "SELECT type, sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        assert observed[0] == object_type
        assert db_store._normalized_schema_sql(observed[1]) == (
            db_store._normalized_schema_sql(expected_sql)
        )
    assert db_store._schema_migration_steps(4) == [
        "schema_v4_to_v5",
        "schema_v5_to_v6",
        "schema_v6_to_v7",
        "schema_v7_to_v8",
        "schema_v8_to_v9",
    ]
    assert db_store._schema_migration_steps(5) == [
        "schema_v5_to_v6",
        "schema_v6_to_v7",
        "schema_v7_to_v8",
        "schema_v8_to_v9",
    ]
    assert db_store._schema_migration_steps(6) == [
        "schema_v6_to_v7",
        "schema_v7_to_v8",
        "schema_v8_to_v9",
    ]
    assert db_store._schema_migration_steps(7) == [
        "schema_v7_to_v8",
        "schema_v8_to_v9",
    ]
    assert db_store._schema_migration_steps(8) == ["schema_v8_to_v9"]
    assert db_store._schema_migration_steps(9) == []


def test_v6_to_current_preserves_v7_payload_rows_refs_and_hydration(isolated_memory):
    path, payloads_before, refs_before = _physical_v6_with_shared_pending_payload()
    preview = db_store.preview_schema_migration(path)

    assert preview["steps"] == [
        "schema_v6_to_v7",
        "schema_v7_to_v8",
        "schema_v8_to_v9",
    ]
    result = db_store.schema_migration_maintenance(
        apply=True,
        confirmation=preview["fingerprint"],
        confirm_no_writers=True,
        db_path=path,
    )

    assert result["pre"]["user_version"] == 6
    assert result["post"]["user_version"] == 9
    assert result["post"]["ready"] is True
    backup_path = Path(result["backup"]["path"])
    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 6
        assert _payload_snapshots(backup_path) == (payloads_before, refs_before)
    finally:
        backup.close()
    assert _payload_snapshots(path) == (payloads_before, refs_before)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        connection.close()
    pending = governance_store.pending_change_sets(limit=10)
    runtime_connection = db_store.get_connection()
    assert runtime_connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert {item["change_set_id"] for item in pending} == {
        "v6-pending-a",
        "v6-pending-b",
    }
    assert pending[0]["proposed_claims"] == pending[1]["proposed_claims"]

    published = governance_store.publish_change_sets(limit=10)
    assert set(published["change_set_ids"]) == {"v6-pending-a", "v6-pending-b"}
    assert runtime_connection.execute(
        "SELECT COUNT(*) FROM change_set_payload_refs"
    ).fetchone()[0] == 0
    assert runtime_connection.execute(
        "SELECT COUNT(*) FROM change_set_payloads"
    ).fetchone()[0] == 0
    manifests = [
        json.loads(row[0])
        for row in runtime_connection.execute(
            "SELECT data_json FROM change_sets ORDER BY change_set_id"
        ).fetchall()
    ]
    assert len(manifests) == 2
    for manifest in manifests:
        assert manifest["payload"]["available"] is False
        assert not any(
            section in manifest
            for section in governance_store._CHANGE_SET_PAYLOAD_SECTIONS
        )

    plan = governance_store.plan_history_retention(
        runtime_connection,
        cutoff="2027-01-01T00:00:00+00:00",
        keep_change_sets=0,
        keep_terminal_jobs=0,
        keep_terminal_outbox=0,
        scan_version_history=False,
        plan_as_of="2027-01-02T00:00:00+00:00",
    )
    with db_store.transaction():
        governance_store.apply_history_retention_plan(
            runtime_connection,
            plan,
            confirmation=plan["fingerprint"],
            plan_as_of=plan["plan_as_of"],
        )
    assert runtime_connection.execute("SELECT COUNT(*) FROM change_sets").fetchone()[0] == 0
    assert runtime_connection.execute(
        "SELECT COUNT(*) FROM change_set_lifecycle_v6"
    ).fetchone()[0] == 0
    assert runtime_connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v7_migration_has_no_python_blob_table_snapshots():
    source = inspect.getsource(db_store._migrate_change_set_payload_schema_v7)

    assert "payload_snapshot" not in source
    assert "final_payloads" not in source
    assert "SELECT payload_sha256, codec, payload_blob" not in source


def test_v7_rebuild_failure_rolls_back_exact_v6_database(
    isolated_memory,
    monkeypatch,
):
    path, _payloads, _refs = _physical_v6_with_shared_pending_payload()
    before = _v6_database_snapshot(path)
    preview = db_store.preview_schema_migration(path)
    real_migrate = db_store._migrate_change_set_payload_schema_v7

    def fail_after_rebuild(connection):
        real_migrate(connection)
        raise RuntimeError("injected v7 failure")

    monkeypatch.setattr(
        db_store,
        "_migrate_change_set_payload_schema_v7",
        fail_after_rebuild,
    )
    with pytest.raises(RuntimeError, match="injected v7 failure"):
        db_store.schema_migration_maintenance(
            apply=True,
            confirmation=preview["fingerprint"],
            confirm_no_writers=True,
            db_path=path,
        )

    assert _v6_database_snapshot(path) == before
    assert db_store.inspect_schema_migration_state(path)["user_version"] == 6


def test_exact_8_mib_roundtrip_after_close_reopen_and_oversize_atomic_rejection(
    isolated_memory,
):
    db_store.init_db()
    accepted, raw = _with_exact_payload_size(
        _change_set("raw-cap-accepted", idempotency_key="raw-cap-idem"),
        governance_store._CHANGE_SET_MAX_PAYLOAD_BYTES,
    )
    assert len(raw) == 8 * 1024 * 1024
    assert len(zlib.compress(raw)) <= governance_store._CHANGE_SET_MAX_STORED_BYTES
    assert governance_store.record_prepared_change_sets([accepted]) == 1
    db_store.close_all_connections()
    db_store.init_db()

    hydrated = governance_store._load_change_set_by_idempotency_key("raw-cap-idem")
    pending = governance_store.pending_change_sets(limit=10)
    assert hydrated["proposed_claims"] == accepted["proposed_claims"]
    assert pending[0]["proposed_claims"] == accepted["proposed_claims"]
    manifest = json.loads(
        db_store.get_connection()
        .execute(
            "SELECT data_json FROM change_sets WHERE change_set_id='raw-cap-accepted'"
        )
        .fetchone()[0]
    )
    assert manifest["payload"]["raw_bytes"] == 8 * 1024 * 1024
    assert manifest["payload"]["sha256"] == hashlib.sha256(raw).hexdigest()

    before_rejection = _row_counts(db_store.get_connection())
    rejected, rejected_raw = _with_exact_payload_size(
        _change_set("raw-cap-rejected"),
        governance_store._CHANGE_SET_MAX_PAYLOAD_BYTES + 1,
    )
    assert len(rejected_raw) == 8 * 1024 * 1024 + 1
    with pytest.raises(governance_store.ChangeSetPayloadTooLarge):
        governance_store.record_prepared_change_sets([rejected])
    assert _row_counts(db_store.get_connection()) == before_rejection


def test_incompressible_stored_cap_rejection_is_atomic(isolated_memory):
    db_store.init_db()
    random_bytes = random.Random(20260809).randbytes(4_800_000)
    entropy = base64.b64encode(random_bytes).decode("ascii")
    change_set = _change_set("stored-cap-rejected")
    change_set["proposed_claims"][0]["claim_text"] = entropy
    _payload, raw = governance_store._canonical_change_set_payload(change_set)
    compressed = zlib.compress(raw)

    assert len(raw) <= governance_store._CHANGE_SET_MAX_PAYLOAD_BYTES
    assert len(compressed) > governance_store._CHANGE_SET_MAX_STORED_BYTES
    with pytest.raises(governance_store.ChangeSetPayloadTooLarge):
        governance_store.record_prepared_change_sets([change_set])
    assert _row_counts(db_store.get_connection()) == {
        "change_sets": 0,
        "change_set_payloads": 0,
        "change_set_payload_refs": 0,
        "change_set_lifecycle_v6": 0,
        "change_set_idempotency": 0,
    }


def test_exact_32_mib_batch_acceptance_and_one_byte_over_atomic_rejection(
    isolated_memory,
):
    db_store.init_db()
    raw_cap = governance_store._CHANGE_SET_MAX_PAYLOAD_BYTES
    exact_batch = [
        _with_exact_payload_size(
            _change_set(
                f"batch-{index}",
                page=f"Concept_{index}.md",
                payload_tag=str(index),
            ),
            raw_cap,
        )[0]
        for index in range(4)
    ]
    small = _change_set(
        "batch-over-b",
        page="Concept_Over_B.md",
        payload_tag="b",
    )
    _payload, small_raw = governance_store._canonical_change_set_payload(small)
    over_batch = [*exact_batch[:3]]
    over_batch.append(
        _with_exact_payload_size(
            _change_set(
                "batch-over-a",
                page="Concept_Over_A.md",
                payload_tag="a",
            ),
            raw_cap - len(small_raw) + 1,
        )[0]
    )
    over_batch.append(small)

    with pytest.raises(governance_store.ChangeSetBatchTooLarge):
        governance_store.record_prepared_change_sets(over_batch)
    assert _row_counts(db_store.get_connection()) == {
        "change_sets": 0,
        "change_set_payloads": 0,
        "change_set_payload_refs": 0,
        "change_set_lifecycle_v6": 0,
        "change_set_idempotency": 0,
    }
    assert governance_store.record_prepared_change_sets(exact_batch) == 4
    assert (
        db_store.get_connection()
        .execute("SELECT SUM(raw_bytes) FROM change_set_payloads")
        .fetchone()[0]
        == 32 * 1024 * 1024
    )


def test_retention_keeps_pending_payload_and_reference(isolated_memory):
    db_store.init_db()
    pending = _change_set(
        "retention-pending",
        created_at="2020-01-01T00:00:00+00:00",
    )
    assert governance_store.record_prepared_change_sets([pending]) == 1
    connection = db_store.get_connection()
    plan = governance_store.plan_history_retention(
        connection,
        cutoff="2025-01-01T00:00:00+00:00",
        keep_change_sets=0,
        keep_terminal_jobs=0,
        keep_terminal_outbox=0,
        scan_version_history=False,
        plan_as_of="2026-08-09T00:00:00+00:00",
    )
    assert not any(
        item["table"] == "change_sets" and item["key"] == "retention-pending"
        for item in plan["candidates"]
    )
    with db_store.transaction():
        governance_store.apply_history_retention_plan(
            connection,
            plan,
            confirmation=plan["fingerprint"],
            plan_as_of=plan["plan_as_of"],
        )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM change_set_payload_refs "
            "WHERE change_set_id='retention-pending'"
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM change_set_payloads").fetchone()[0]
        == 1
    )
    assert governance_store.pending_change_sets(limit=10)[0]["change_set_id"] == (
        "retention-pending"
    )
