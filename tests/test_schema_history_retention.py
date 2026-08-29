import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from filelock import FileLock, Timeout

from vector_lake import db_store, governance_store
from vector_lake.tool_governance_maintenance import history_retention_maintenance


OLD = "2020-01-01T00:00:00+00:00"


def _record_hash(record: dict) -> str:
    payload = governance_store._canonical_record_json(record)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _seed_version_family(
    conn: sqlite3.Connection,
    *,
    kind: str,
    object_id: str,
    family_id: str,
    page_key: str,
) -> list[str]:
    if kind == "claim":
        table = "claim_versions"
        version_field = "claim_version_id"
        id_field = "claim_id"
        family_field = "claim_family_id"
        canonical_table = "claims"
    else:
        table = "evidence_versions"
        version_field = "evidence_version_id"
        id_field = "evidence_id"
        family_field = "evidence_family_id"
        canonical_table = "evidence"

    version_ids = []
    records = []
    for version_no in range(1, 4):
        record = {
            id_field: object_id,
            family_field: family_id,
            "locator": {"page_key": page_key, "block_index": version_no},
            "text": f"{kind}-version-{version_no}",
        }
        version_id = f"{kind}_version_{object_id}_{version_no}"
        conn.execute(
            f"INSERT INTO {table} "
            f"({version_field}, {id_field}, {family_field}, page_key, version_no, "
            "record_hash, data_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                object_id,
                family_id,
                page_key,
                version_no,
                _record_hash(record),
                governance_store._canonical_record_json(record),
                OLD,
            ),
        )
        version_ids.append(version_id)
        records.append(record)

    current_record = records[0]
    if canonical_table == "claims":
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, 'Active', ?, ?)",
            (
                object_id,
                current_record["text"],
                json.dumps(current_record, ensure_ascii=False),
                OLD,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO evidence (evidence_id, data_json, updated_at) VALUES (?, ?, ?)",
            (object_id, json.dumps(current_record, ensure_ascii=False), OLD),
        )
    identity = {
        "record_kind": kind,
        "record_id": object_id,
        "page_key": page_key,
    }
    conn.execute(
        "INSERT OR IGNORE INTO canonical_identities "
        "(record_kind, record_id, page_key, identity_origin, data_json, recorded_at) "
        "VALUES (?, ?, ?, 'test_seed', ?, ?)",
        (
            kind,
            object_id,
            page_key,
            governance_store._canonical_record_json(identity),
            OLD,
        ),
    )
    return version_ids


def _insert_job(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    *,
    at: str,
    retries: int = 0,
    page_key: str = "PageJob",
    task_packet_path: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO jobs "
        "(job_id, task_type, payload, status, retries, created_at, updated_at, "
        "available_at, task_packet_path, completed_at) "
        "VALUES (?, 'ingest', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            json.dumps({"page_key": page_key}),
            status,
            retries,
            at,
            at,
            at,
            task_packet_path,
            at if status not in {"queued", "dispatched", "awaiting_subagent"} else None,
        ),
    )


def _insert_cleanup(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
) -> None:
    conn.execute(
        "INSERT INTO ingest_task_cleanup "
        "(job_id, task_packet_path, expected_task_id, status, available_at, "
        "created_at, updated_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            f"C:/scratch/{job_id}.json",
            f"task-{job_id}",
            status,
            OLD,
            OLD,
            OLD,
            OLD if status == "completed" else None,
        ),
    )


def _exists(
    conn: sqlite3.Connection,
    table: str,
    key: str,
    value: object,
) -> bool:
    return bool(
        conn.execute(
            f"SELECT 1 FROM {table} WHERE {key} = ?",
            (value,),
        ).fetchone()
    )


def _drop_runtime_generation_triggers(conn: sqlite3.Connection) -> None:
    for object_type, name, _sql in db_store._RUNTIME_GENERATION_SCHEMA_OBJECTS_V3:
        if object_type == "trigger":
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def _downgrade_payload_contract_to_v6(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS projection_runtime_v9")
    conn.execute("DROP TABLE IF EXISTS embedding_metadata_v8")
    conn.execute("DROP TABLE IF EXISTS search_projection_state_v8")
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(mutation_outbox)").fetchall()
    }
    for column_name in (
        "poison_attempt_count",
        "transient_attempt_count",
        "last_error_code",
        "first_transient_at",
    ):
        if column_name in columns:
            conn.execute(f'ALTER TABLE mutation_outbox DROP COLUMN "{column_name}"')
    payloads = conn.execute(
        "SELECT payload_sha256, codec, payload_blob, raw_bytes, stored_bytes, "
        "created_at FROM change_set_payloads"
    ).fetchall()
    refs = conn.execute(
        "SELECT change_set_id, payload_sha256, created_at "
        "FROM change_set_payload_refs"
    ).fetchall()
    conn.execute("DROP TABLE change_set_payload_refs")
    conn.execute("DROP TABLE change_set_payloads")
    conn.execute(db_store._CHANGE_SET_PAYLOADS_TABLE_SCHEMA_V6)
    conn.execute(db_store._CHANGE_SET_PAYLOAD_REFS_TABLE_SCHEMA_V6)
    conn.execute(db_store._CHANGE_SET_PAYLOAD_REFS_INDEX_SCHEMA_V6)
    conn.executemany(
        "INSERT INTO change_set_payloads "
        "(payload_sha256, codec, payload_blob, raw_bytes, stored_bytes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        payloads,
    )
    conn.executemany(
        "INSERT INTO change_set_payload_refs "
        "(change_set_id, payload_sha256, created_at) VALUES (?, ?, ?)",
        refs,
    )


def _downgrade_runtime_generation_to_v2(path) -> None:
    conn = db_store.get_connection()
    with db_store.transaction():
        _downgrade_payload_contract_to_v6(conn)
        _drop_runtime_generation_triggers(conn)
        conn.execute("DELETE FROM schema_migrations WHERE version >= 3")
        conn.execute("PRAGMA user_version = 2")
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))


def _downgrade_cleanup_contract(path, version: int) -> None:
    conn = db_store.get_connection()
    with db_store.transaction():
        _downgrade_payload_contract_to_v6(conn)
        conn.execute("DROP TABLE ingest_task_cleanup")
        conn.execute(
            "CREATE TABLE ingest_task_cleanup ("
            "cleanup_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "job_id TEXT NOT NULL, "
            "task_packet_path TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO ingest_task_cleanup (job_id, task_packet_path) "
            "VALUES ('job-legacy-cleanup', 'C:/scratch/task-legacy-cleanup.json')"
        )
        if version == 2:
            _drop_runtime_generation_triggers(conn)
            conn.execute("DROP TABLE runtime_generations")
        conn.execute("DELETE FROM schema_migrations WHERE version > ?", (version,))
        conn.execute(f"PRAGMA user_version = {version}")
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))


def _create_v5_duplicate_indexes(
    conn: sqlite3.Connection,
    *,
    wrong_index: str | None = None,
) -> None:
    date_column = "sentiment" if wrong_index == "idx_date" else "event_date"
    entity_column = "sentiment" if wrong_index == "idx_entity" else "entity_id"
    conn.execute(f"CREATE INDEX idx_date ON timeline_events({date_column})")
    conn.execute(f"CREATE INDEX idx_entity ON timeline_events({entity_column})")


def _create_deferred_external_consumer_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE wiki_embeddings ("
        "node_key TEXT PRIMARY KEY, embedding_json TEXT, updated_at TEXT, "
        "content_hash TEXT, model TEXT)"
    )
    conn.execute(
        "CREATE TABLE embedding_jobs ("
        "node_key TEXT, job_id TEXT, content_hash TEXT, model TEXT, "
        "text_version TEXT, estimated_tokens INTEGER, state TEXT, "
        "attempts INTEGER, force_single INTEGER, next_attempt_at REAL, "
        "lease_owner TEXT, lease_until REAL, last_http_status INTEGER, "
        "last_error_class TEXT, last_error TEXT, created_at REAL, updated_at REAL)"
    )
    conn.execute(
        "CREATE TABLE embedding_rate_events ("
        "request_id TEXT, reserved_at REAL, token_count INTEGER, "
        "item_count INTEGER, outcome TEXT, http_status INTEGER, completed_at REAL)"
    )
    conn.execute("INSERT INTO wiki_embeddings (node_key) VALUES ('legacy-embedding')")
    conn.execute("INSERT INTO embedding_jobs (node_key) VALUES ('legacy-job')")
    conn.execute(
        "INSERT INTO embedding_rate_events (request_id) VALUES ('legacy-request')"
    )


def _downgrade_duplicate_indexes_to_v4(
    path,
    *,
    wrong_index: str | None = None,
) -> None:
    conn = db_store.get_connection()
    with db_store.transaction():
        _downgrade_payload_contract_to_v6(conn)
        _create_v5_duplicate_indexes(conn, wrong_index=wrong_index)
        _create_deferred_external_consumer_tables(conn)
        conn.execute("DELETE FROM schema_migrations WHERE version >= 5")
        conn.execute("PRAGMA user_version = 4")
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))


def _downgrade_identity_registry_to_v1(path) -> None:
    conn = db_store.get_connection()
    with db_store.transaction():
        _downgrade_payload_contract_to_v6(conn)
        _drop_runtime_generation_triggers(conn)
        conn.execute(
            "DROP TRIGGER IF EXISTS trg_canonical_identities_append_only_update"
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS trg_canonical_identities_append_only_delete"
        )
        conn.execute("DROP TABLE canonical_identities")
        conn.execute("DROP TABLE runtime_generations")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 2")
        conn.execute("PRAGMA user_version = 1")
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))


def _run_controlled_existing_schema_migration(path) -> None:
    maintenance_lock = FileLock(
        str(path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME),
        timeout=0,
    )
    connection = db_store.get_connection()
    with maintenance_lock:
        with db_store._controlled_schema_v5_transaction(
            connection,
            maintenance_lock,
        ):
            current_version = db_store._validate_schema_migration_state(connection)
            if current_version < 2:
                db_store._migrate_canonical_identity_schema_v2(connection)
            if current_version < 3:
                db_store._migrate_runtime_generation_schema_v3(connection)
            if current_version < 4:
                db_store._migrate_ingest_task_cleanup_schema_v4(connection)
            if current_version < 4:
                applied_at = datetime.now(timezone.utc).isoformat()
                for version in range(current_version + 1, 5):
                    name, checksum = db_store._SCHEMA_MIGRATIONS[version]
                    connection.execute(
                        "INSERT INTO schema_migrations "
                        "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (version, name, checksum, applied_at),
                    )
                connection.execute("PRAGMA user_version = 4")
            db_store._apply_controlled_schema_v5_migration(
                connection,
                maintenance_lock=maintenance_lock,
            )
            db_store._apply_controlled_schema_v6_migration(
                connection,
                maintenance_lock=maintenance_lock,
            )
            db_store._apply_controlled_schema_v7_migration(
                connection,
                maintenance_lock=maintenance_lock,
            )
            db_store._apply_controlled_schema_v8_migration(
                connection,
                maintenance_lock=maintenance_lock,
            )
            db_store._apply_controlled_schema_v9_migration(
                connection,
                maintenance_lock=maintenance_lock,
            )


def test_schema_v2_checksum_is_derived_from_normalized_ddl_contract():
    contract = db_store._CANONICAL_IDENTITIES_SCHEMA_V2
    checksum = db_store._SCHEMA_MIGRATIONS[2][1]

    assert checksum == db_store._schema_contract_checksum(contract)
    assert checksum == db_store._schema_contract_checksum(
        tuple(f"  {statement}\n" for statement in contract)
    )
    changed_contract = (
        contract[0].replace("page_key TEXT NOT NULL", "page_key TEXT"),
        *contract[1:],
    )
    assert db_store._schema_contract_checksum(changed_contract) != checksum


def test_schema_v3_checksum_is_derived_from_runtime_generation_contract():
    contract = db_store._RUNTIME_GENERATION_SCHEMA_V3
    checksum = db_store._SCHEMA_MIGRATIONS[3][1]

    assert checksum == db_store._schema_contract_checksum(contract)
    assert checksum == db_store._schema_contract_checksum(
        tuple(f"  {statement}\n" for statement in contract)
    )
    changed_contract = (
        contract[0],
        contract[1].replace("generation = generation + 1", "generation = 0"),
        *contract[2:],
    )
    assert db_store._schema_contract_checksum(changed_contract) != checksum


def test_schema_v4_checksum_is_derived_from_cleanup_contract():
    contract = db_store._INGEST_TASK_CLEANUP_SCHEMA_V4
    checksum = db_store._SCHEMA_MIGRATIONS[4][1]

    assert checksum == db_store._schema_contract_checksum(contract)
    changed_contract = (
        contract[0].replace("lease_token TEXT", "lease_token BLOB"),
        contract[1],
    )
    assert db_store._schema_contract_checksum(changed_contract) != checksum


def test_schema_v5_checksum_is_derived_from_duplicate_index_absence_contract():
    contract = db_store._DUPLICATE_INDEX_CLEANUP_SCHEMA_V5
    checksum = db_store._SCHEMA_MIGRATIONS[5][1]

    assert checksum == db_store._schema_contract_checksum(contract)
    changed_contract = (
        contract[0].replace("ABSENT INDEX", "PRESENT INDEX"),
        *contract[1:],
    )
    assert db_store._schema_contract_checksum(changed_contract) != checksum


def test_schema_v6_checksum_covers_terminal_status_immutability():
    contract = db_store._CHANGE_SET_HISTORY_SCHEMA_V6
    checksum = db_store._SCHEMA_MIGRATIONS[6][1]

    assert checksum == db_store._schema_contract_checksum(contract)
    weakened_trigger = contract[5].replace(
        "WHEN OLD.status IN (",
        "WHEN OLD.terminal_at IS NOT NULL AND OLD.status IN (",
    )
    assert weakened_trigger != contract[5]
    changed_contract = (*contract[:5], weakened_trigger, *contract[6:])
    assert db_store._schema_contract_checksum(changed_contract) != checksum


@pytest.mark.parametrize(
    ("damage", "issue"),
    [
        (
            "missing_table",
            "canonical_identity_schema_missing:table:canonical_identities",
        ),
        (
            "missing_index",
            "canonical_identity_schema_missing:index:idx_canonical_identities_page",
        ),
        (
            "wrong_trigger",
            "canonical_identity_schema_sql_mismatch:"
            "trg_canonical_identities_append_only_delete",
        ),
        (
            "missing_constraint",
            "canonical_identity_schema_sql_mismatch:canonical_identities",
        ),
    ],
)
def test_schema_inspection_and_init_reject_identity_contract_damage(
    isolated_memory,
    damage,
    issue,
):
    db_store.init_db()
    path = db_store.get_db_path()
    with db_store.transaction() as conn:
        if damage == "missing_table":
            conn.execute("DROP TABLE canonical_identities")
        elif damage == "missing_index":
            conn.execute("DROP INDEX idx_canonical_identities_page")
        elif damage == "wrong_trigger":
            conn.execute("DROP TRIGGER trg_canonical_identities_append_only_delete")
            conn.execute(
                "CREATE TRIGGER trg_canonical_identities_append_only_delete "
                "BEFORE INSERT ON canonical_identities BEGIN SELECT 1; END"
            )
        else:
            conn.execute("DROP TABLE canonical_identities")
            conn.execute(
                db_store._CANONICAL_IDENTITIES_SCHEMA_V2[0].replace(
                    "CHECK(length(trim(page_key)) > 0),",
                    "",
                )
            )
            for statement in db_store._CANONICAL_IDENTITIES_SCHEMA_V2[1:]:
                conn.execute(statement)

    state = db_store.inspect_schema_migration_state(path)

    assert state["ready"] is False
    assert state["status"] == "invalid"
    assert issue in state["issues"]

    with pytest.raises(RuntimeError, match="identity contract is invalid"):
        db_store.init_db()
    db_store.close_all_connections()

    after = db_store.inspect_schema_migration_state(path)
    assert issue in after["issues"]


def test_schema_ledger_is_durable_and_read_only_inspection_is_current(isolated_memory):
    db_store.init_db()
    path = db_store.get_db_path()

    state = db_store.inspect_schema_migration_state(path)

    assert state["ready"] is True
    assert state["status"] == "ready"
    assert state["user_version"] == db_store._SCHEMA_VERSION == 9
    expected_versions = list(range(1, db_store._SCHEMA_VERSION + 1))
    assert [item["version"] for item in state["ledger"]] == expected_versions
    assert [item["name"] for item in state["ledger"]] == [
        db_store._SCHEMA_MIGRATIONS[version][0] for version in expected_versions
    ]
    assert [item["checksum"] for item in state["ledger"]] == [
        db_store._SCHEMA_MIGRATIONS[version][1] for version in expected_versions
    ]
    assert all(item["applied_at"] for item in state["ledger"])


@pytest.mark.parametrize(
    ("damage", "issue"),
    [
        (
            "missing_trigger",
            "runtime_generation_schema_missing:trigger:"
            "trg_entities_generation_v3_insert",
        ),
        (
            "wrong_trigger",
            "runtime_generation_schema_sql_mismatch:trg_entities_generation_v3_insert",
        ),
        (
            "missing_registry",
            "runtime_generation_registry_missing:entities",
        ),
        (
            "unexpected_trigger",
            "runtime_generation_schema_unexpected:trigger:"
            "trg_entities_generation_v1_shadow",
        ),
    ],
)
def test_schema_inspection_and_init_reject_runtime_generation_damage(
    isolated_memory,
    damage,
    issue,
):
    db_store.init_db()
    path = db_store.get_db_path()
    trigger_name = db_store._runtime_generation_trigger_name("entities", "insert")
    with db_store.transaction() as conn:
        if damage in {"missing_trigger", "wrong_trigger"}:
            conn.execute(f"DROP TRIGGER {trigger_name}")
        if damage == "wrong_trigger":
            conn.execute(
                f"CREATE TRIGGER {trigger_name} AFTER INSERT ON entities "
                "BEGIN SELECT 1; END"
            )
        elif damage == "missing_registry":
            conn.execute("DELETE FROM runtime_generations WHERE surface = 'entities'")
        elif damage == "unexpected_trigger":
            conn.execute(
                "CREATE TRIGGER trg_entities_generation_v1_shadow "
                "AFTER INSERT ON entities BEGIN SELECT 1; END"
            )

    state = db_store.inspect_schema_migration_state(path)

    assert state["ready"] is False
    assert state["status"] == "invalid"
    assert issue in state["issues"]
    with pytest.raises(
        RuntimeError,
        match="runtime generation contract is invalid",
    ):
        db_store.init_db()


def test_existing_schema_v2_migrates_runtime_generation_triggers_atomically(
    isolated_memory,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_runtime_generation_to_v2(path)

    before = db_store.inspect_schema_migration_state(path)
    assert before["user_version"] == 2
    assert before["status"] == "invalid"

    _run_controlled_existing_schema_migration(path)
    state = db_store.inspect_schema_migration_state(path)

    assert state["ready"] is True
    assert state["user_version"] == 9
    assert [item["version"] for item in state["ledger"]] == list(range(1, 10))


def test_schema_v3_trigger_migration_failure_rolls_back_ledger_and_ddl(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_runtime_generation_to_v2(path)
    original = db_store._RUNTIME_GENERATION_TRIGGER_SCHEMA_V3
    monkeypatch.setattr(
        db_store,
        "_RUNTIME_GENERATION_TRIGGER_SCHEMA_V3",
        (original[0], "CREATE TRIGGER invalid runtime generation syntax"),
    )

    with pytest.raises(sqlite3.OperationalError):
        _run_controlled_existing_schema_migration(path)
    db_store.close_all_connections()

    raw = sqlite3.connect(path)
    try:
        user_version = raw.execute("PRAGMA user_version").fetchone()[0]
        ledger = raw.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        trigger_count = raw.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name LIKE '%_generation_v3_%'"
        ).fetchone()[0]
    finally:
        raw.close()

    assert user_version == 2
    assert ledger == [(1,), (2,)]
    assert trigger_count == 0


@pytest.mark.parametrize("legacy_version", [2, 3])
def test_incomplete_cleanup_table_migrates_to_schema_v4(
    isolated_memory,
    legacy_version,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_cleanup_contract(path, legacy_version)

    before = db_store.inspect_schema_migration_state(path)
    assert before["user_version"] == legacy_version
    assert [item["version"] for item in before["ledger"]] == list(
        range(1, legacy_version + 1)
    )
    assert before["ready"] is False

    _run_controlled_existing_schema_migration(path)
    state = db_store.inspect_schema_migration_state(path)
    row = db_store.get_connection().execute(
        "SELECT * FROM ingest_task_cleanup WHERE job_id = 'job-legacy-cleanup'"
    ).fetchone()

    assert state["ready"] is True
    assert state["user_version"] == 9
    assert [item["version"] for item in state["ledger"]] == list(range(1, 10))
    assert db_store._ingest_task_cleanup_schema_issues(
        db_store.get_connection()
    ) == []
    assert row["expected_task_id"] == "task-legacy-cleanup"
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["lease_generation"] == 0
    assert row["available_at"]
    assert row["created_at"]
    assert row["updated_at"]


def test_schema_v4_cleanup_contract_has_all_columns_defaults_and_indexes(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    columns = {
        str(row["name"]): row
        for row in conn.execute("PRAGMA table_info('ingest_task_cleanup')").fetchall()
    }

    assert tuple(columns) == tuple(
        item[0] for item in db_store._INGEST_TASK_CLEANUP_COLUMN_CONTRACT_V4
    )
    for name, column_type, not_null, primary_key in (
        db_store._INGEST_TASK_CLEANUP_COLUMN_CONTRACT_V4
    ):
        row = columns[name]
        assert str(row["type"]).casefold() == column_type.casefold()
        assert bool(row["notnull"]) is not_null
        assert bool(row["pk"]) is primary_key
    for name, expected in db_store._INGEST_TASK_CLEANUP_DEFAULTS_V4.items():
        assert db_store._normalized_schema_default(
            columns[name]["dflt_value"]
        ) == db_store._normalized_schema_default(expected)
    assert db_store._ingest_task_cleanup_has_identity_index(conn)
    ready_index = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'index' AND name = 'idx_ingest_task_cleanup_ready'"
    ).fetchone()
    assert ready_index is not None
    assert db_store._normalized_schema_sql(
        ready_index["sql"]
    ) == db_store._normalized_schema_sql(
        db_store._INGEST_TASK_CLEANUP_READY_INDEX_SCHEMA_V4
    )


@pytest.mark.parametrize(
    ("damage", "issue"),
    [
        (
            "wrong_default",
            "ingest_task_cleanup_schema_default_mismatch:column:status",
        ),
        (
            "missing_unique",
            "ingest_task_cleanup_schema_missing:unique:job_id_task_packet_path",
        ),
        (
            "partial_unique",
            "ingest_task_cleanup_schema_missing:unique:job_id_task_packet_path",
        ),
        (
            "unexpected_column",
            "ingest_task_cleanup_schema_unexpected:column:shadow_state",
        ),
        (
            "missing_ready_index",
            "ingest_task_cleanup_schema_missing:index:idx_ingest_task_cleanup_ready",
        ),
    ],
)
def test_schema_inspection_and_init_reject_malformed_v4_cleanup_contract(
    isolated_memory,
    damage,
    issue,
):
    db_store.init_db()
    path = db_store.get_db_path()
    with db_store.transaction() as conn:
        if damage == "missing_ready_index":
            conn.execute("DROP INDEX idx_ingest_task_cleanup_ready")
        else:
            conn.execute("DROP TABLE ingest_task_cleanup")
            table_sql = db_store._INGEST_TASK_CLEANUP_TABLE_SCHEMA_V4
            if damage == "wrong_default":
                table_sql = table_sql.replace(
                    "status TEXT NOT NULL DEFAULT 'pending'",
                    "status TEXT NOT NULL DEFAULT 'queued'",
                )
            elif damage in {"missing_unique", "partial_unique"}:
                table_sql = table_sql.replace(
                    ",\n    UNIQUE(job_id, task_packet_path)",
                    "",
                )
            elif damage == "unexpected_column":
                table_sql = table_sql.replace(
                    "lease_generation INTEGER NOT NULL DEFAULT 0,",
                    "shadow_state TEXT,\n"
                    "    lease_generation INTEGER NOT NULL DEFAULT 0,",
                )
            conn.execute(table_sql)
            conn.execute(db_store._INGEST_TASK_CLEANUP_READY_INDEX_SCHEMA_V4)
            if damage == "partial_unique":
                conn.execute(
                    "CREATE UNIQUE INDEX idx_ingest_task_cleanup_identity_v4 "
                    "ON ingest_task_cleanup(job_id, task_packet_path) "
                    "WHERE status = 'pending'"
                )

    state = db_store.inspect_schema_migration_state(path)

    assert state["ready"] is False
    assert state["status"] == "invalid"
    assert issue in state["issues"]
    with pytest.raises(
        RuntimeError,
        match="ingest task cleanup contract is invalid",
    ):
        db_store.init_db()


def test_schema_v4_migration_failure_rolls_back_columns_indexes_and_ledger(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_cleanup_contract(path, 3)
    monkeypatch.setattr(
        db_store,
        "_INGEST_TASK_CLEANUP_READY_INDEX_SCHEMA_V4",
        "CREATE INDEX invalid cleanup index syntax",
    )

    with pytest.raises(sqlite3.OperationalError):
        _run_controlled_existing_schema_migration(path)
    db_store.close_all_connections()

    raw = sqlite3.connect(path)
    try:
        user_version = raw.execute("PRAGMA user_version").fetchone()[0]
        ledger = raw.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        columns = [
            str(row[1])
            for row in raw.execute("PRAGMA table_info('ingest_task_cleanup')").fetchall()
        ]
        indexes = raw.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'ingest_task_cleanup'"
        ).fetchall()
    finally:
        raw.close()

    assert user_version == 3
    assert ledger == [
        (version, *db_store._SCHEMA_MIGRATIONS[version])
        for version in (1, 2, 3)
    ]
    assert columns == ["cleanup_id", "job_id", "task_packet_path"]
    assert indexes == []


def _v5_duplicate_index_names(conn: sqlite3.Connection) -> set[str]:
    names = set()
    for index_name in db_store._DUPLICATE_INDEXES_V5:
        names.update(
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name = ? COLLATE NOCASE",
                (index_name,),
            ).fetchall()
        )
    return names


def _deferred_external_consumer_table_names(conn: sqlite3.Connection) -> set[str]:
    table_names = db_store._DEFERRED_EXTERNAL_CONSUMER_TABLES_V5
    placeholders = ", ".join("?" for _ in table_names)
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            f"AND name IN ({placeholders})",
            tuple(sorted(table_names)),
        ).fetchall()
    }


def test_existing_schema_v4_removes_duplicate_indexes_but_preserves_deferred_tables(
    isolated_memory,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_duplicate_indexes_to_v4(path)

    with pytest.raises(
        RuntimeError,
        match="Database schema upgrade required: 4->9",
    ):
        db_store.init_db()
    blocked = db_store.get_connection()
    assert int(blocked.execute("PRAGMA user_version").fetchone()[0]) == 4
    assert _v5_duplicate_index_names(blocked) == set(db_store._DUPLICATE_INDEXES_V5)
    held_lock = FileLock(
        str(path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME),
        timeout=0,
    )
    with held_lock:
        with pytest.raises(
            RuntimeError,
            match="Database schema upgrade required: 4->9",
        ):
            db_store._init_db_once(str(path.resolve()))

    _run_controlled_existing_schema_migration(path)
    conn = db_store.get_connection()
    state = db_store.inspect_schema_migration_connection(conn, path)

    assert state["ready"] is True
    assert state["user_version"] == 9
    assert [item["version"] for item in state["ledger"]] == list(range(1, 10))
    assert _v5_duplicate_index_names(conn) == set()
    assert _deferred_external_consumer_table_names(conn) == set(
        db_store._DEFERRED_EXTERNAL_CONSUMER_TABLES_V5
    )
    assert (
        conn.execute("SELECT node_key FROM wiki_embeddings").fetchone()[0]
        == "legacy-embedding"
    )
    assert (
        conn.execute("SELECT node_key FROM embedding_jobs").fetchone()[0]
        == "legacy-job"
    )
    assert (
        conn.execute("SELECT request_id FROM embedding_rate_events").fetchone()[0]
        == "legacy-request"
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    date_plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT event_date FROM timeline_events "
            "ORDER BY event_date DESC LIMIT 10"
        ).fetchall()
    )
    entity_plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT event_date FROM timeline_events "
            "WHERE entity_id = 'Entity_Test'"
        ).fetchall()
    )
    assert "idx_timeline_date" in date_plan
    assert "idx_timeline_entity" in entity_plan

    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))
    db_store.init_db()
    conn = db_store.get_connection()
    assert _v5_duplicate_index_names(conn) == set()
    assert _deferred_external_consumer_table_names(conn) == set(
        db_store._DEFERRED_EXTERNAL_CONSUMER_TABLES_V5
    )


def test_cached_init_refuses_a_database_downgraded_to_v4(isolated_memory):
    db_store.init_db()
    path = db_store.get_db_path()
    connection = db_store.get_connection()
    with db_store.transaction():
        _downgrade_payload_contract_to_v6(connection)
        _create_v5_duplicate_indexes(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version > 4")
        connection.execute("PRAGMA user_version = 4")

    assert str(path.resolve()) in db_store._INITIALIZED_DB_PATHS
    with pytest.raises(
        RuntimeError,
        match="Database schema upgrade required: 4->9",
    ):
        db_store.init_db()

    assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 4
    assert _v5_duplicate_index_names(connection) == set(
        db_store._DUPLICATE_INDEXES_V5
    )


def test_cached_init_holds_schema_migration_guard_through_validation(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    path = db_store.get_db_path()
    lock_path = path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME
    original = db_store._validate_cached_identity_state
    observations = []

    def validate_while_guarded(connection):
        with pytest.raises(Timeout):
            with FileLock(str(lock_path), timeout=0):
                pass
        observations.append("guarded")
        return original(connection)

    monkeypatch.setattr(
        db_store,
        "_validate_cached_identity_state",
        validate_while_guarded,
    )

    db_store.init_db()

    assert observations == ["guarded"]


def test_controlled_schema_v5_entry_requires_caller_owned_transaction(
    isolated_memory,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_duplicate_indexes_to_v4(path)
    conn = db_store.get_connection()
    maintenance_lock = FileLock(
        str(path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME),
        timeout=0,
    )

    with maintenance_lock:
        with pytest.raises(RuntimeError, match="requires an active caller transaction"):
            db_store._apply_controlled_schema_v5_migration(
                conn,
                maintenance_lock=maintenance_lock,
            )

        with db_store._controlled_schema_v5_transaction(conn, maintenance_lock):
            outsider = sqlite3.connect(path, timeout=0)
            try:
                outsider.execute("PRAGMA busy_timeout=0")
                with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                    outsider.execute("CREATE TABLE writer_must_be_blocked (id INTEGER)")
            finally:
                outsider.close()
            db_store._apply_controlled_schema_v5_migration(
                conn,
                maintenance_lock=maintenance_lock,
            )

    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 5
    assert _v5_duplicate_index_names(conn) == set()


def test_controlled_schema_v5_entry_rejects_a_forged_lock(isolated_memory):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_duplicate_indexes_to_v4(path)
    conn = db_store.get_connection()

    class ForgedLock:
        is_locked = True
        lock_file = str(path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME)

    with db_store.transaction():
        with pytest.raises(RuntimeError, match="migration lock is not held"):
            db_store._apply_controlled_schema_v5_migration(
                conn,
                maintenance_lock=ForgedLock(),
            )

    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 4
    assert _v5_duplicate_index_names(conn) == set(
        db_store._DUPLICATE_INDEXES_V5
    )


def test_controlled_schema_v5_entry_rejects_a_generic_transaction(
    isolated_memory,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_duplicate_indexes_to_v4(path)
    conn = db_store.get_connection()
    maintenance_lock = FileLock(
        str(path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME),
        timeout=0,
    )

    with maintenance_lock:
        with db_store.transaction():
            with pytest.raises(RuntimeError, match="lock-bound transaction"):
                db_store._apply_controlled_schema_v5_migration(
                    conn,
                    maintenance_lock=maintenance_lock,
                )

    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 4
    assert _v5_duplicate_index_names(conn) == set(
        db_store._DUPLICATE_INDEXES_V5
    )


def test_controlled_schema_v5_transaction_rolls_back_if_lock_released_before_commit(
    isolated_memory,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_duplicate_indexes_to_v4(path)
    conn = db_store.get_connection()
    maintenance_lock = FileLock(
        str(path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME),
        timeout=0,
    )
    maintenance_lock.acquire()

    with pytest.raises(RuntimeError, match="migration lock is not held"):
        with db_store._controlled_schema_v5_transaction(conn, maintenance_lock):
            db_store._apply_controlled_schema_v5_migration(
                conn,
                maintenance_lock=maintenance_lock,
            )
            maintenance_lock.release()

    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 4
    assert _v5_duplicate_index_names(conn) == set(
        db_store._DUPLICATE_INDEXES_V5
    )


@pytest.mark.parametrize("wrong_index", ["idx_date", "idx_entity"])
def test_schema_v5_migration_refuses_unexpected_duplicate_index_shape_atomically(
    isolated_memory,
    wrong_index,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_duplicate_indexes_to_v4(path, wrong_index=wrong_index)

    with pytest.raises(
        RuntimeError,
        match=rf"duplicate_index_migration_shape_mismatch:index:{wrong_index}",
    ):
        _run_controlled_existing_schema_migration(path)

    conn = db_store.get_connection()
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 4
    assert _v5_duplicate_index_names(conn) == set(db_store._DUPLICATE_INDEXES_V5)
    assert _deferred_external_consumer_table_names(conn) == set(
        db_store._DEFERRED_EXTERNAL_CONSUMER_TABLES_V5
    )


def test_schema_v5_preflight_rejects_case_variant_with_wrong_shape_atomically(
    isolated_memory,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_duplicate_indexes_to_v4(path)
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute('DROP INDEX "idx_date"')
        conn.execute('CREATE INDEX "IDX_DATE" ON timeline_events(sentiment)')
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))

    with pytest.raises(
        RuntimeError,
        match="duplicate_index_migration_shape_mismatch:index:idx_date",
    ):
        _run_controlled_existing_schema_migration(path)

    conn = db_store.get_connection()
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 4
    assert _v5_duplicate_index_names(conn) == {"IDX_DATE", "idx_entity"}


def test_schema_v5_post_drop_failure_rolls_back_indexes_ledger_and_external_rows(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_duplicate_indexes_to_v4(path)
    original = db_store._duplicate_index_cleanup_v5_issues

    def fail_after_drop(conn):
        if not _v5_duplicate_index_names(conn):
            return ["injected_post_drop_failure"]
        return original(conn)

    monkeypatch.setattr(db_store, "_duplicate_index_cleanup_v5_issues", fail_after_drop)

    with pytest.raises(RuntimeError, match="injected_post_drop_failure"):
        _run_controlled_existing_schema_migration(path)

    conn = db_store.get_connection()
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 4
    assert _v5_duplicate_index_names(conn) == set(db_store._DUPLICATE_INDEXES_V5)
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT node_key FROM wiki_embeddings"
    ).fetchone()[0] == "legacy-embedding"
    assert conn.execute("SELECT node_key FROM embedding_jobs").fetchone()[0] == "legacy-job"
    assert conn.execute(
        "SELECT request_id FROM embedding_rate_events"
    ).fetchone()[0] == "legacy-request"


@pytest.mark.parametrize(
    ("replacement_index", "wrong_column"),
    [
        ("idx_timeline_date", "sentiment"),
        ("idx_timeline_entity", "action"),
    ],
)
def test_schema_v5_migration_refuses_invalid_replacement_index_atomically(
    isolated_memory,
    replacement_index,
    wrong_column,
):
    db_store.init_db()
    path = db_store.get_db_path()
    _downgrade_duplicate_indexes_to_v4(path)
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(f'DROP INDEX "{replacement_index}"')
        conn.execute(
            f'CREATE INDEX "{replacement_index}" '
            f'ON timeline_events("{wrong_column}")'
        )
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))

    with pytest.raises(
        RuntimeError,
        match=(
            "duplicate_index_migration_replacement_invalid:index:"
            + replacement_index
        ),
    ):
        _run_controlled_existing_schema_migration(path)

    conn = db_store.get_connection()
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 4
    assert _v5_duplicate_index_names(conn) == set(db_store._DUPLICATE_INDEXES_V5)


def test_schema_v5_allows_deferred_external_consumer_tables(isolated_memory):
    db_store.init_db()
    path = db_store.get_db_path()
    with db_store.transaction() as conn:
        _create_deferred_external_consumer_tables(conn)

    state = db_store.inspect_schema_migration_state(path)

    assert state["ready"] is True
    assert state["issues"] == []
    db_store.init_db()
    assert _deferred_external_consumer_table_names(db_store.get_connection()) == set(
        db_store._DEFERRED_EXTERNAL_CONSUMER_TABLES_V5
    )


@pytest.mark.parametrize(
    ("object_name", "create_sql", "issue"),
    [
        (
            "idx_date",
            "CREATE INDEX idx_date ON timeline_events(event_date)",
            "duplicate_index_schema_unexpected:index:idx_date",
        ),
        (
            "idx_entity",
            "CREATE INDEX idx_entity ON timeline_events(entity_id)",
            "duplicate_index_schema_unexpected:index:idx_entity",
        ),
        (
            "IDX_DATE",
            "CREATE INDEX IDX_DATE ON timeline_events(event_date)",
            "duplicate_index_schema_unexpected:index:IDX_DATE",
        ),
    ],
)
def test_schema_inspection_and_init_reject_reintroduced_v5_duplicate_index(
    isolated_memory,
    object_name,
    create_sql,
    issue,
):
    db_store.init_db()
    path = db_store.get_db_path()
    with db_store.transaction() as conn:
        conn.execute(create_sql)

    state = db_store.inspect_schema_migration_state(path)

    assert state["ready"] is False
    assert issue in state["issues"]
    with pytest.raises(
        RuntimeError,
        match=rf"duplicate index cleanup contract is invalid.*{object_name}",
    ):
        db_store.init_db()


def test_first_cleanup_enqueue_claim_and_complete_succeeds(isolated_memory):
    db_store.init_db()
    packet_path = isolated_memory / "scratch" / "task-first-cleanup.json"
    with db_store.transaction() as conn:
        _insert_job(
            conn,
            "job-first-cleanup",
            "completed",
            at=OLD,
            task_packet_path=str(packet_path),
        )
        cleanup_id = db_store.enqueue_ingest_task_cleanup(
            "job-first-cleanup",
            str(packet_path),
        )

    claimed = db_store.claim_ingest_task_cleanup(
        limit=1,
        lease_seconds=60,
        lease_owner="schema-v4-test",
    )

    assert len(claimed) == 1
    assert claimed[0]["cleanup_id"] == cleanup_id
    assert claimed[0]["expected_task_id"] == "task-first-cleanup"
    assert claimed[0]["lease_generation"] == 1
    assert db_store.complete_ingest_task_cleanup(
        cleanup_id,
        claimed[0]["lease_owner"],
        claimed[0]["lease_token"],
        claimed[0]["lease_generation"],
    )
    conn = db_store.get_connection()
    cleanup = conn.execute(
        "SELECT status, completed_at, lease_owner, lease_token "
        "FROM ingest_task_cleanup WHERE cleanup_id = ?",
        (cleanup_id,),
    ).fetchone()
    job = conn.execute(
        "SELECT task_packet_path FROM jobs WHERE job_id = 'job-first-cleanup'"
    ).fetchone()
    assert cleanup["status"] == "completed"
    assert cleanup["completed_at"]
    assert cleanup["lease_owner"] is None
    assert cleanup["lease_token"] is None
    assert job["task_packet_path"] is None

def test_schema_v2_backfills_current_and_version_identity_owners(isolated_memory):
    db_store.init_db()
    path = db_store.get_db_path()
    conn = db_store.get_connection()
    with db_store.transaction():
        _seed_version_family(
            conn,
            kind="claim",
            object_id="claim-migrated",
            family_id="claim-family-migrated",
            page_key="PageMigrated",
        )
        _seed_version_family(
            conn,
            kind="evidence",
            object_id="evidence-migrated",
            family_id="evidence-family-migrated",
            page_key="PageMigrated",
        )
    _downgrade_identity_registry_to_v1(path)

    _run_controlled_existing_schema_migration(path)
    rows = (
        db_store.get_connection()
        .execute(
            "SELECT record_kind, record_id, page_key, identity_origin "
            "FROM canonical_identities ORDER BY record_kind, record_id"
        )
        .fetchall()
    )

    assert [tuple(row) for row in rows] == [
        ("claim", "claim-migrated", "PageMigrated", "schema_v2_backfill"),
        ("evidence", "evidence-migrated", "PageMigrated", "schema_v2_backfill"),
    ]
    assert db_store.inspect_schema_migration_state(path)["ready"] is True


@pytest.mark.parametrize("surviving_surface", ["current", "version"])
def test_schema_v2_backfills_current_only_or_version_only_identity(
    isolated_memory,
    surviving_surface,
):
    db_store.init_db()
    path = db_store.get_db_path()
    conn = db_store.get_connection()
    with db_store.transaction():
        _seed_version_family(
            conn,
            kind="claim",
            object_id="claim-one-surface",
            family_id="claim-family-one-surface",
            page_key="PageOneSurface",
        )
        if surviving_surface == "current":
            conn.execute(
                "DELETE FROM claim_versions WHERE claim_id = 'claim-one-surface'"
            )
        else:
            conn.execute("DELETE FROM claims WHERE claim_id = 'claim-one-surface'")
    _downgrade_identity_registry_to_v1(path)

    _run_controlled_existing_schema_migration(path)
    owner = (
        db_store.get_connection()
        .execute(
            "SELECT page_key, identity_origin FROM canonical_identities "
            "WHERE record_kind = 'claim' AND record_id = 'claim-one-surface'"
        )
        .fetchone()
    )

    assert tuple(owner) == ("PageOneSurface", "schema_v2_backfill")


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("missing", "missing an identity registry owner"),
        ("inconsistent", "registry page 'PageOther'"),
    ],
)
def test_existing_schema_v2_rejects_missing_or_inconsistent_identity_coverage(
    isolated_memory,
    damage,
    message,
):
    db_store.init_db()
    path = db_store.get_db_path()
    conn = db_store.get_connection()
    with db_store.transaction():
        _seed_version_family(
            conn,
            kind="claim",
            object_id="claim-v2-damaged",
            family_id="claim-family-v2-damaged",
            page_key="PageOriginal",
        )
        if damage == "missing":
            conn.execute("DROP TRIGGER trg_canonical_identities_append_only_delete")
            conn.execute(
                "DELETE FROM canonical_identities WHERE record_kind = 'claim' "
                "AND record_id = 'claim-v2-damaged'"
            )
            conn.execute(db_store._CANONICAL_IDENTITIES_SCHEMA_V2[4])
        else:
            conn.execute("DROP TRIGGER trg_canonical_identities_append_only_update")
            identity = {
                "record_kind": "claim",
                "record_id": "claim-v2-damaged",
                "page_key": "PageOther",
            }
            conn.execute(
                "UPDATE canonical_identities SET page_key = ?, data_json = ? "
                "WHERE record_kind = 'claim' AND record_id = 'claim-v2-damaged'",
                (
                    "PageOther",
                    governance_store._canonical_record_json(identity),
                ),
            )
            conn.execute(db_store._CANONICAL_IDENTITIES_SCHEMA_V2[3])

    state = db_store.inspect_schema_migration_state(path)
    assert state["ready"] is False
    assert any(
        issue.startswith("canonical_identity_integrity:") for issue in state["issues"]
    )

    with pytest.raises(RuntimeError, match=message):
        db_store.init_db()


def test_existing_schema_v2_allows_registry_only_historical_identity(
    isolated_memory,
):
    db_store.init_db()
    path = db_store.get_db_path()
    identity = {
        "record_kind": "claim",
        "record_id": "claim-registry-only",
        "page_key": "PageDeleted",
    }
    with db_store.transaction() as conn:
        conn.execute(
            "INSERT INTO canonical_identities "
            "(record_kind, record_id, page_key, identity_origin, data_json, recorded_at) "
            "VALUES ('claim', 'claim-registry-only', 'PageDeleted', "
            "'historical_test', ?, '2026-07-27T00:00:00+00:00')",
            (governance_store._canonical_record_json(identity),),
        )
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))

    db_store.init_db()

    assert (
        db_store.get_connection()
        .execute(
            "SELECT page_key FROM canonical_identities WHERE record_kind = 'claim' "
            "AND record_id = 'claim-registry-only'"
        )
        .fetchone()["page_key"]
        == "PageDeleted"
    )


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("invalid_json", "invalid identity JSON"),
        ("conflicting_page", "conflicting page ownership"),
    ],
)
def test_schema_v2_backfill_fails_closed_and_rolls_back(
    isolated_memory,
    damage,
    message,
):
    db_store.init_db()
    path = db_store.get_db_path()
    conn = db_store.get_connection()
    with db_store.transaction():
        _seed_version_family(
            conn,
            kind="claim",
            object_id="claim-damaged",
            family_id="claim-family-damaged",
            page_key="PageOriginal",
        )
        if damage == "invalid_json":
            conn.execute(
                "UPDATE claim_versions SET data_json = '{' "
                "WHERE claim_id = 'claim-damaged' AND version_no = 2"
            )
        else:
            record = {
                "claim_id": "claim-damaged",
                "claim_family_id": "claim-family-damaged",
                "locator": {"page_key": "PageOther", "block_index": 2},
            }
            conn.execute(
                "UPDATE claim_versions SET page_key = ?, data_json = ? "
                "WHERE claim_id = ? AND version_no = 2",
                (
                    "PageOther",
                    governance_store._canonical_record_json(record),
                    "claim-damaged",
                ),
            )
    _downgrade_identity_registry_to_v1(path)

    with pytest.raises(RuntimeError, match=message):
        _run_controlled_existing_schema_migration(path)
    db_store.close_all_connections()

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            raw.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'canonical_identities'"
            ).fetchone()
            is None
        )
        assert raw.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,)]
    finally:
        raw.close()


def test_read_only_schema_inspection_and_retention_preview_do_not_create_database(
    isolated_memory,
):
    path = db_store.peek_db_path()
    assert not path.exists()

    state = db_store.inspect_schema_migration_state()
    preview = json.loads(history_retention_maintenance())

    assert state["status"] == "missing"
    assert state["issues"] == ["database_missing"]
    assert preview["dry_run"] is True
    assert preview["applied"] is False
    assert preview["preview_error"] == "schema_not_ready:missing"
    assert not path.exists()


def test_init_refuses_active_schema_migration_window_without_creating_database(
    isolated_memory,
):
    path = db_store.peek_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME

    with FileLock(str(lock_path), timeout=0):
        with pytest.raises(
            RuntimeError,
            match="schema migration maintenance window is active",
        ):
            db_store.init_db()

    assert not path.exists()


def test_schema_migration_failure_rolls_back_ledger_and_user_version(isolated_memory):
    path = db_store.peek_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path)
    raw.execute("CREATE VIEW entities AS SELECT 'entity' AS entity_id")
    raw.commit()
    raw.close()

    with pytest.raises(sqlite3.OperationalError):
        db_store.init_db()
    db_store.close_all_connections()

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
        tables = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        raw.close()
    assert "schema_migrations" not in tables
    assert "vec_embeddings" not in tables


def test_add_column_ignores_only_explicit_duplicate_column_error():
    class FailingConnection:
        def __init__(self, message: str):
            self.message = message

        def execute(self, _sql: str):
            raise sqlite3.OperationalError(self.message)

    db_store._add_column_if_missing(
        FailingConnection("duplicate column name: status"),
        "entities",
        "status",
        "TEXT",
    )
    with pytest.raises(sqlite3.OperationalError, match="malformed"):
        db_store._add_column_if_missing(
            FailingConnection("database disk image is malformed"),
            "entities",
            "status",
            "TEXT",
        )


def test_history_retention_preview_and_apply_are_bounded_and_reference_safe(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    recent = datetime.now(timezone.utc).isoformat()

    with db_store.transaction():
        change_sets = {
            "cs-terminal-old": {"status": "published"},
            "cs-terminal-keep": {"status": "published"},
            "cs-referenced-old": {"status": "published"},
            "cs-active-old": {
                "status": "pending",
                "affected_pages": ["PagePending.md"],
                "proposed_claims": [
                    {
                        "claim_id": "claim-pending",
                        "claim_family_id": "claim-family-pending",
                        "locator": {"page_key": "PagePending"},
                    }
                ],
                "proposed_evidence": [
                    {
                        "evidence_id": "evidence-pending",
                        "evidence_family_id": "evidence-family-pending",
                        "locator": {"page_key": "PagePending"},
                    }
                ],
            },
        }
        for change_set_id, body in change_sets.items():
            body = {"change_set_id": change_set_id, **body}
            at = recent if change_set_id == "cs-terminal-keep" else OLD
            raw_body = json.dumps(body)
            conn.execute(
                "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
                "VALUES (?, ?, ?)",
                (change_set_id, raw_body, at),
            )
            status = body["status"]
            conn.execute(
                "INSERT INTO change_set_lifecycle_v6 "
                "(change_set_id, status, created_at, terminal_at, time_source, "
                "payload_guard_sha256) VALUES (?, ?, ?, ?, 'test_seed', ?)",
                (
                    change_set_id,
                    status,
                    at,
                    at if status == "published" else None,
                    hashlib.sha256(raw_body.encode("utf-8")).hexdigest(),
                ),
            )
            conn.execute(
                "INSERT INTO change_set_idempotency "
                "(idempotency_key, change_set_id, created_at) VALUES (?, ?, ?)",
                (f"idem-{change_set_id}", change_set_id, at),
            )
        queue = {
            "item_id": "queue-ref",
            "status": "pending",
            "change_set_id": "cs-referenced-old",
        }
        conn.execute(
            "INSERT INTO governance_queue (item_id, data_json, updated_at) "
            "VALUES ('queue-ref', ?, ?)",
            (json.dumps(queue), OLD),
        )

        _insert_job(conn, "job-terminal-keep", "completed", at=recent)
        _insert_job(conn, "job-terminal-old", "completed", at=OLD)
        _insert_job(conn, "job-failed-exhausted", "failed", at=OLD, retries=3)
        _insert_job(conn, "job-active", "queued", at=OLD, page_key="PageActive")
        _insert_job(conn, "job-failed-retry", "failed", at=OLD, retries=2)
        _insert_job(
            conn,
            "job-packet",
            "completed",
            at=OLD,
            task_packet_path="C:/scratch/job-packet.json",
        )
        _insert_job(conn, "job-cleanup-pending", "completed", at=OLD)
        _insert_cleanup(conn, "job-cleanup-pending", "pending")
        _insert_job(conn, "job-cleanup-completed", "completed", at=OLD)
        _insert_cleanup(conn, "job-cleanup-completed", "completed")

        def insert_outbox(filename: str, status: str, at: str) -> int:
            cursor = conn.execute(
                "INSERT INTO mutation_outbox "
                "(filename, mutation_type, status, created_at, available_at, completed_at) "
                "VALUES (?, 'update', ?, ?, ?, ?)",
                (filename, status, at, at, at if status != "pending" else None),
            )
            return int(cursor.lastrowid)

        outbox_keep = insert_outbox("PageKeep.md", "completed", recent)
        outbox_old = insert_outbox("PageOld.md", "completed", OLD)
        outbox_failed = insert_outbox("PageFailed.md", "failed", OLD)
        outbox_referenced = insert_outbox("PageReferenced.md", "completed", OLD)
        outbox_active = insert_outbox("PageActive.md", "pending", OLD)
        conn.execute(
            "UPDATE mutation_outbox SET superseded_by = ? WHERE id = ?",
            (outbox_referenced, outbox_active),
        )

        claim_safe = _seed_version_family(
            conn,
            kind="claim",
            object_id="claim-safe",
            family_id="claim-family-safe",
            page_key="PageSafe",
        )
        claim_active = _seed_version_family(
            conn,
            kind="claim",
            object_id="claim-active",
            family_id="claim-family-active",
            page_key="PageActive",
        )
        claim_pending = _seed_version_family(
            conn,
            kind="claim",
            object_id="claim-pending",
            family_id="claim-family-pending",
            page_key="PagePending",
        )
        evidence_safe = _seed_version_family(
            conn,
            kind="evidence",
            object_id="evidence-safe",
            family_id="evidence-family-safe",
            page_key="PageSafe",
        )
        evidence_active = _seed_version_family(
            conn,
            kind="evidence",
            object_id="evidence-active",
            family_id="evidence-family-active",
            page_key="PageActive",
        )
        evidence_pending = _seed_version_family(
            conn,
            kind="evidence",
            object_id="evidence-pending",
            family_id="evidence-family-pending",
            page_key="PagePending",
        )

        conn.execute(
            "INSERT INTO embedding_runs "
            "(run_id, status, model, started_at, updated_at) "
            "VALUES ('embedding-run', 'completed', 'test', ?, ?)",
            (OLD, OLD),
        )
        conn.execute(
            "INSERT INTO embedding_rate_reservations "
            "(reservation_id, reserved_at, token_count) VALUES ('reservation', 1.0, 10)"
        )

    options = {
        "ttl_days": 1,
        "batch_size": 100,
        "keep_change_sets": 1,
        "keep_terminal_jobs": 1,
        "keep_terminal_outbox": 1,
        "keep_versions_per_family": 1,
    }
    counts_before = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "change_sets",
            "jobs",
            "mutation_outbox",
            "claim_versions",
            "evidence_versions",
            "canonical_identities",
            "embedding_runs",
            "embedding_rate_reservations",
        )
    }

    preview = json.loads(history_retention_maintenance(dry_run=True, **options))

    assert preview["applied"] is False
    assert preview["selected_counts"] == {
        "change_sets": 1,
        "jobs": 3,
        "mutation_outbox": 2,
        "claim_versions": 1,
        "evidence_versions": 1,
    }
    assert preview["selected_samples"]["change_sets"] == ["cs-terminal-old"]
    assert set(preview["selected_samples"]["jobs"]) == {
        "job-terminal-old",
        "job-failed-exhausted",
        "job-cleanup-completed",
    }
    assert set(preview["selected_samples"]["mutation_outbox"]) == {
        outbox_old,
        outbox_failed,
    }
    assert preview["selected_samples"]["claim_versions"] == [claim_safe[1]]
    assert preview["selected_samples"]["evidence_versions"] == [evidence_safe[1]]
    assert counts_before == {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in counts_before
    }

    applied = json.loads(
        history_retention_maintenance(
            dry_run=False,
            plan_as_of=preview["plan_as_of"],
            confirmation=preview["fingerprint"],
            **options,
        )
    )

    replayed = json.loads(
        history_retention_maintenance(
            dry_run=False,
            plan_as_of=preview["plan_as_of"],
            confirmation=preview["fingerprint"],
            **options,
        )
    )
    with pytest.raises(RuntimeError, match="receipt options do not match"):
        history_retention_maintenance(
            dry_run=False,
            plan_as_of=preview["plan_as_of"],
            confirmation=preview["fingerprint"],
            **{**options, "batch_size": options["batch_size"] - 1},
        )

    assert applied["applied"] is True
    assert replayed["replayed_receipt"] is True
    assert {
        key: value
        for key, value in applied["deleted_counts"].items()
        if key != "change_set_payloads"
    } == preview["selected_counts"]
    assert applied["deleted_counts"]["change_set_payloads"] == 0
    assert not _exists(conn, "change_sets", "change_set_id", "cs-terminal-old")
    assert _exists(conn, "change_sets", "change_set_id", "cs-terminal-keep")
    assert _exists(conn, "change_sets", "change_set_id", "cs-referenced-old")
    assert _exists(conn, "change_sets", "change_set_id", "cs-active-old")
    assert not _exists(
        conn,
        "change_set_idempotency",
        "change_set_id",
        "cs-terminal-old",
    )
    assert _exists(
        conn,
        "change_set_idempotency",
        "change_set_id",
        "cs-referenced-old",
    )

    for job_id in (
        "job-terminal-old",
        "job-failed-exhausted",
        "job-cleanup-completed",
    ):
        assert not _exists(conn, "jobs", "job_id", job_id)
    for job_id in (
        "job-terminal-keep",
        "job-active",
        "job-failed-retry",
        "job-packet",
        "job-cleanup-pending",
    ):
        assert _exists(conn, "jobs", "job_id", job_id)
    assert not _exists(
        conn,
        "ingest_task_cleanup",
        "job_id",
        "job-cleanup-completed",
    )
    assert _exists(conn, "ingest_task_cleanup", "job_id", "job-cleanup-pending")

    assert not _exists(conn, "mutation_outbox", "id", outbox_old)
    assert not _exists(conn, "mutation_outbox", "id", outbox_failed)
    assert _exists(conn, "mutation_outbox", "id", outbox_keep)
    assert _exists(conn, "mutation_outbox", "id", outbox_referenced)
    assert _exists(conn, "mutation_outbox", "id", outbox_active)

    assert not _exists(conn, "claim_versions", "claim_version_id", claim_safe[1])
    assert _exists(conn, "claim_versions", "claim_version_id", claim_safe[0])
    assert _exists(conn, "claim_versions", "claim_version_id", claim_safe[2])
    assert all(
        _exists(conn, "claim_versions", "claim_version_id", version_id)
        for version_id in claim_active + claim_pending
    )
    assert not _exists(
        conn,
        "evidence_versions",
        "evidence_version_id",
        evidence_safe[1],
    )
    assert _exists(
        conn,
        "evidence_versions",
        "evidence_version_id",
        evidence_safe[0],
    )
    assert _exists(
        conn,
        "evidence_versions",
        "evidence_version_id",
        evidence_safe[2],
    )
    assert all(
        _exists(conn, "evidence_versions", "evidence_version_id", version_id)
        for version_id in evidence_active + evidence_pending
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM canonical_identities").fetchone()[0]
        == counts_before["canonical_identities"]
    )
    assert conn.execute("SELECT COUNT(*) FROM embedding_runs").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM embedding_rate_reservations").fetchone()[0]
        == 1
    )


def test_schema_inspection_returns_invalid_when_read_only_connect_fails(
    isolated_memory,
    monkeypatch,
):
    path = db_store.peek_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"present")

    def fail_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(db_store.sqlite3, "connect", fail_connect)

    state = db_store.inspect_schema_migration_state(path)

    assert state["ready"] is False
    assert state["status"] == "invalid"
    assert state["issues"] == ["schema_inspection_failed:unable to open database file"]


def test_history_retention_fails_closed_on_unknown_references_and_timestamps(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    cutoff = datetime.now(timezone.utc).isoformat()

    with db_store.transaction():
        for change_set_id, updated_at in (
            ("cs-valid-old", OLD),
            ("cs-invalid-time", "not-a-date"),
        ):
            conn.execute(
                "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
                "VALUES (?, ?, ?)",
                (
                    change_set_id,
                    json.dumps({"change_set_id": change_set_id, "status": "published"}),
                    updated_at,
                ),
            )
        conn.execute(
            "INSERT INTO governance_queue (item_id, data_json, updated_at) "
            "VALUES ('malformed-queue', '{', ?)",
            (OLD,),
        )
        _insert_job(conn, "job-invalid-time", "completed", at="not-a-date")
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at) "
            "VALUES ('job-malformed-active', 'ingest', '{', 'queued', 0, ?, ?)",
            (OLD, OLD),
        )
        conn.execute(
            "INSERT INTO mutation_outbox "
            "(filename, mutation_type, status, created_at, available_at, completed_at) "
            "VALUES ('InvalidTime.md', 'update', 'completed', ?, ?, ?)",
            ("not-a-date", "not-a-date", "not-a-date"),
        )
        _seed_version_family(
            conn,
            kind="claim",
            object_id="claim-blocked",
            family_id="claim-family-blocked",
            page_key="PageBlocked",
        )

    plan = governance_store.plan_history_retention(
        conn,
        cutoff=cutoff,
        batch_size=100,
        keep_change_sets=0,
        keep_terminal_jobs=0,
        keep_terminal_outbox=0,
        keep_versions_per_family=1,
    )

    assert plan["selected_ids"]["change_sets"] == []
    assert plan["selected_ids"]["jobs"] == []
    assert plan["selected_ids"]["mutation_outbox"] == []
    assert plan["selected_ids"]["claim_versions"] == []
    assert plan["active_protection_counts"]["block_version_retention"] == 1
    assert (
        plan["version_skip_counts"]["claim_versions"]["blocked_by_unknown_active_work"]
        == 1
    )
