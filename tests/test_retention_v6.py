import hashlib
import json
import sqlite3

import pytest
from filelock import FileLock

from vector_lake import db_store, governance_store
from vector_lake import tool_governance_maintenance as maintenance


OLD = "2020-01-01T00:00:00+00:00"
RECENT = "2026-08-03T00:00:00+00:00"


def _drop_v6_contract(conn) -> None:
    conn.execute("DROP TRIGGER trg_change_set_terminal_v6_immutable")
    for index_name in (
        "idx_change_set_payload_refs_payload_v6",
        "idx_change_set_lifecycle_retention_v6",
        "idx_jobs_retention_v6",
        "idx_mutation_outbox_retention_v6",
        "idx_claim_versions_retention_v6",
        "idx_evidence_versions_retention_v6",
    ):
        conn.execute(f"DROP INDEX {index_name}")
    conn.execute("DROP TABLE history_retention_runs_v6")
    conn.execute("DROP TABLE change_set_payload_refs")
    conn.execute("DROP TABLE change_set_payloads")
    conn.execute("DROP TABLE change_set_lifecycle_v6")
    conn.execute("DELETE FROM schema_migrations WHERE version >= 6")
    conn.execute("PRAGMA user_version = 5")


def _controlled_v6_migrate(path) -> None:
    lock = FileLock(
        str(path.parent / db_store._SCHEMA_MIGRATION_LOCK_FILENAME),
        timeout=0,
    )
    conn = db_store.get_connection()
    with lock:
        with db_store._controlled_schema_v5_transaction(conn, lock):
            db_store._apply_controlled_schema_v6_migration(
                conn,
                maintenance_lock=lock,
            )
            db_store._apply_controlled_schema_v7_migration(
                conn,
                maintenance_lock=lock,
            )


def _insert_change_set(
    conn,
    change_set_id: str,
    body: dict,
    *,
    updated_at: str,
    terminal_at: str | None,
) -> str:
    body = {"change_set_id": change_set_id, **body}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        "INSERT INTO change_sets (change_set_id, data_json, updated_at) VALUES (?, ?, ?)",
        (change_set_id, raw, updated_at),
    )
    conn.execute(
        "INSERT INTO change_set_idempotency "
        "(idempotency_key, change_set_id, created_at) VALUES (?, ?, ?)",
        (f"idem-{change_set_id}", change_set_id, updated_at),
    )
    status = str(body.get("status") or "pending")
    conn.execute(
        "INSERT INTO change_set_lifecycle_v6 "
        "(change_set_id, status, created_at, terminal_at, time_source, "
        "payload_guard_sha256) VALUES (?, ?, ?, ?, 'test_seed', ?)",
        (
            change_set_id,
            status,
            body.get("created_at"),
            terminal_at,
            hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        ),
    )
    return raw


def _retention_plan(conn, *, rows: int = 100) -> dict:
    return governance_store.plan_history_retention(
        conn,
        cutoff="2026-08-01T00:00:00+00:00",
        plan_as_of=RECENT,
        batch_size=rows,
        keep_change_sets=0,
        keep_terminal_jobs=0,
        keep_terminal_outbox=0,
        keep_versions_per_family=1,
    )


def _seed_claim_version_family(
    conn,
    *,
    prefix: str = "keyset",
    payload_suffix: str = "",
) -> list[str]:
    claim_id = f"claim-{prefix}"
    family_id = f"family-{prefix}"
    page_key = f"Concept_{prefix}"
    version_ids = []
    records = []
    for version_no in range(1, 4):
        record = {
            "claim_id": claim_id,
            "claim_family_id": family_id,
            "claim_text": f"version-{version_no}{payload_suffix}",
            "locator": {"page_key": page_key, "block_index": version_no},
        }
        data_json = governance_store._canonical_record_json(record)
        version_id = f"claim-version-{prefix}-{version_no}"
        conn.execute(
            "INSERT INTO claim_versions "
            "(claim_version_id, claim_id, claim_family_id, page_key, version_no, "
            "record_hash, data_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                claim_id,
                family_id,
                page_key,
                version_no,
                hashlib.sha256(data_json.encode("utf-8")).hexdigest(),
                data_json,
                OLD,
            ),
        )
        version_ids.append(version_id)
        records.append(record)
    conn.execute(
        "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) "
        "VALUES (?, ?, 'Active', ?, ?)",
        (
            claim_id,
            records[0]["claim_text"],
            governance_store._canonical_record_json(records[0]),
            OLD,
        ),
    )
    return version_ids


def test_retention_rejects_more_than_500_rows_per_batch(isolated_memory):
    db_store.init_db()

    with pytest.raises(ValueError, match="between 1 and 500"):
        _retention_plan(db_store.get_connection(), rows=501)


def test_v5_to_v6_backfill_preserves_blob_and_uses_business_time(isolated_memory):
    db_store.init_db()
    path = db_store.get_db_path()
    conn = db_store.get_connection()
    with db_store.transaction():
        _drop_v6_contract(conn)
        body = {
            "change_set_id": "legacy-business-time",
            "status": "published",
            "created_at": OLD,
            "published_at": OLD,
            "requires_human_review": True,
            "proposed_entities": [{"entity_id": "e"}],
        }
        raw = json.dumps(body, ensure_ascii=False, indent=1)
        conn.execute(
            "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
            "VALUES ('legacy-business-time', ?, ?)",
            (raw, RECENT),
        )
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))

    _controlled_v6_migrate(path)

    conn = db_store.get_connection()
    assert conn.execute(
        "SELECT data_json FROM change_sets WHERE change_set_id = 'legacy-business-time'"
    ).fetchone()[0] == raw
    lifecycle = conn.execute(
        "SELECT status, terminal_at, time_source FROM change_set_lifecycle_v6 "
        "WHERE change_set_id = 'legacy-business-time'"
    ).fetchone()
    assert tuple(lifecycle) == ("published", OLD, "published_at")
    assert db_store.inspect_schema_migration_state(path)["ready"] is True


def test_v6_backfill_streams_large_payload_guard(isolated_memory, monkeypatch):
    db_store.init_db()
    path = db_store.get_db_path()
    conn = db_store.get_connection()
    raw = json.dumps(
        {
            "change_set_id": "legacy-large-payload",
            "status": "published",
            "created_at": OLD,
            "published_at": OLD,
            "requires_human_review": True,
            "proposed_entities": [{"content": "x" * (5 * 1024 * 1024)}],
        },
        separators=(",", ":"),
    )
    expected_guard = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with db_store.transaction():
        _drop_v6_contract(conn)
        conn.execute(
            "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
            "VALUES ('legacy-large-payload', ?, ?)",
            (raw, RECENT),
        )
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))
    monkeypatch.setattr(
        db_store,
        "_change_set_lifecycle_from_legacy_v6",
        lambda *_args, **_kwargs: pytest.fail("legacy payload was materialized"),
    )

    _controlled_v6_migrate(path)

    lifecycle = db_store.get_connection().execute(
        "SELECT terminal_at, time_source, payload_guard_sha256 "
        "FROM change_set_lifecycle_v6 WHERE change_set_id = 'legacy-large-payload'"
    ).fetchone()
    assert tuple(lifecycle) == (OLD, "published_at", expected_guard)


def test_v6_backfill_never_uses_updated_at_for_unknown_terminal_time(
    isolated_memory,
):
    db_store.init_db()
    path = db_store.get_db_path()
    conn = db_store.get_connection()
    with db_store.transaction():
        _drop_v6_contract(conn)
        raw = json.dumps(
            {
                "change_set_id": "legacy-unknown-time",
                "status": "published",
                "requires_human_review": True,
            }
        )
        conn.execute(
            "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
            "VALUES ('legacy-unknown-time', ?, ?)",
            (raw, OLD),
        )
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(path.resolve()))

    _controlled_v6_migrate(path)

    lifecycle = db_store.get_connection().execute(
        "SELECT terminal_at, time_source FROM change_set_lifecycle_v6 "
        "WHERE change_set_id = 'legacy-unknown-time'"
    ).fetchone()
    assert tuple(lifecycle) == (None, "unknown_v6_backfill")
    with pytest.raises(
        sqlite3.IntegrityError,
        match="change-set terminal lifecycle is immutable",
    ):
        with db_store.transaction() as conn:
            conn.execute(
                "UPDATE change_set_lifecycle_v6 SET terminal_at = ? "
                "WHERE change_set_id = 'legacy-unknown-time'",
                (OLD,),
            )
    plan = _retention_plan(db_store.get_connection())
    assert plan["selected_ids"]["change_sets"] == []


@pytest.mark.parametrize(
    "terminal_status",
    ("applied", "cancelled", "failed", "published", "rejected", "superseded"),
)
@pytest.mark.parametrize("terminal_at", (None, OLD))
def test_terminal_lifecycle_fields_are_immutable_by_status(
    isolated_memory,
    terminal_status,
    terminal_at,
):
    db_store.init_db()
    conn = db_store.get_connection()
    change_set_id = f"terminal-{terminal_status}-{terminal_at is None}"
    with db_store.transaction():
        _insert_change_set(
            conn,
            change_set_id,
            {"status": terminal_status, "created_at": OLD},
            updated_at=OLD,
            terminal_at=terminal_at,
        )

    replacement_status = "rejected" if terminal_status != "rejected" else "published"
    with pytest.raises(
        sqlite3.IntegrityError,
        match="change-set terminal lifecycle is immutable",
    ):
        with db_store.transaction():
            conn.execute(
                "UPDATE change_set_lifecycle_v6 SET status = ? "
                "WHERE change_set_id = ?",
                (replacement_status, change_set_id),
            )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="change-set terminal lifecycle is immutable",
    ):
        with db_store.transaction():
            conn.execute(
                "UPDATE change_set_lifecycle_v6 SET terminal_at = ? "
                "WHERE change_set_id = ?",
                (RECENT, change_set_id),
            )

    observed = conn.execute(
        "SELECT status, terminal_at FROM change_set_lifecycle_v6 "
        "WHERE change_set_id = ?",
        (change_set_id,),
    ).fetchone()
    assert tuple(observed) == (terminal_status, terminal_at)


@pytest.mark.parametrize(
    "terminal_status",
    ("applied", "cancelled", "failed", "published", "rejected", "superseded"),
)
def test_pending_lifecycle_can_transition_to_terminal_once(
    isolated_memory,
    terminal_status,
):
    db_store.init_db()
    conn = db_store.get_connection()
    change_set_id = f"pending-to-{terminal_status}"
    with db_store.transaction():
        _insert_change_set(
            conn,
            change_set_id,
            {"status": "pending", "created_at": OLD},
            updated_at=OLD,
            terminal_at=None,
        )
        conn.execute(
            "UPDATE change_set_lifecycle_v6 SET status = ?, terminal_at = ? "
            "WHERE change_set_id = ?",
            (terminal_status, OLD, change_set_id),
        )

    observed = conn.execute(
        "SELECT status, terminal_at FROM change_set_lifecycle_v6 "
        "WHERE change_set_id = ?",
        (change_set_id,),
    ).fetchone()
    assert tuple(observed) == (terminal_status, OLD)
    with pytest.raises(
        sqlite3.IntegrityError,
        match="change-set terminal lifecycle is immutable",
    ):
        with db_store.transaction():
            conn.execute(
                "UPDATE change_set_lifecycle_v6 SET terminal_at = ? "
                "WHERE change_set_id = ?",
                (RECENT, change_set_id),
            )


def test_retention_uses_terminal_time_not_storage_updated_at(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _insert_change_set(
            conn,
            "old-business-recent-storage",
            {"status": "published", "created_at": OLD},
            updated_at=RECENT,
            terminal_at=OLD,
        )
        _insert_change_set(
            conn,
            "recent-business-old-storage",
            {"status": "published", "created_at": OLD},
            updated_at=OLD,
            terminal_at=RECENT,
        )

    plan = _retention_plan(conn)
    assert "storage_rowid" not in json.dumps(plan, sort_keys=True)

    assert plan["selected_ids"]["change_sets"] == [
        "old-business-recent-storage"
    ]


def test_retention_apply_rejects_drift_and_rolls_back(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        raw = _insert_change_set(
            conn,
            "drifted",
            {"status": "published", "created_at": OLD},
            updated_at=OLD,
            terminal_at=OLD,
        )
    plan = _retention_plan(conn)

    with pytest.raises(RuntimeError, match="drifted|changed"):
        with db_store.transaction():
            conn.execute(
                "UPDATE change_sets SET data_json = data_json || ' ' "
                "WHERE change_set_id = 'drifted'"
            )
            governance_store.apply_history_retention_plan(
                conn,
                plan,
                confirmation=plan["fingerprint"],
                plan_as_of=plan["plan_as_of"],
            )

    assert conn.execute(
        "SELECT data_json FROM change_sets WHERE change_set_id = 'drifted'"
    ).fetchone()[0] == raw


def test_retention_receipt_makes_exact_replay_idempotent(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _insert_change_set(
            conn,
            "receipt-row",
            {"status": "published", "created_at": OLD},
            updated_at=OLD,
            terminal_at=OLD,
        )
    plan = _retention_plan(conn)
    with db_store.transaction():
        first = governance_store.apply_history_retention_plan(
            conn,
            plan,
            confirmation=plan["fingerprint"],
            plan_as_of=plan["plan_as_of"],
        )
    with db_store.transaction():
        second = governance_store.apply_history_retention_plan(
            conn,
            plan,
            confirmation=plan["fingerprint"],
            plan_as_of=plan["plan_as_of"],
        )
    assert first == second
    assert first["change_sets"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM history_retention_runs_v6"
    ).fetchone()[0] == 1


def test_retention_global_row_limit_spans_tables(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(2):
            _insert_change_set(
                conn,
                f"limited-{index}",
                {"status": "published", "created_at": OLD},
                updated_at=OLD,
                terminal_at=OLD,
            )
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at, "
            "available_at, completed_at) VALUES "
            "('limited-job','ingest','{}','completed',0,?,?,?,?)",
            (OLD, OLD, OLD, OLD),
        )

    plan = _retention_plan(conn, rows=2)

    assert plan["selected_count_total"] == 2
    assert sum(plan["selected_counts"].values()) == 2


def test_retention_logical_size_counts_utf8_bytes(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    payload = "中" * 100
    with db_store.transaction():
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at, "
            "available_at, completed_at) VALUES "
            "('utf8-job','ingest',?,'completed',0,?,?,?,?)",
            (payload, OLD, OLD, OLD, OLD),
        )

    metadata = governance_store._history_candidate_metadata(conn, "jobs", "utf8-job")

    assert metadata["logical_bytes"] == len(payload.encode("utf-8")) + 256


def test_terminal_jobs_and_outbox_without_completed_at_are_protected(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at) "
            "VALUES ('unknown-job','ingest','{}','completed',0,?,?)",
            (OLD, OLD),
        )
        conn.execute(
            "INSERT INTO mutation_outbox "
            "(filename, mutation_type, status, created_at, completed_at) "
            "VALUES ('Unknown.md','update','completed',?,NULL)",
            (OLD,),
        )

    plan = _retention_plan(conn)

    assert plan["selected_ids"]["jobs"] == []
    assert plan["selected_ids"]["mutation_outbox"] == []


def test_version_retention_cursor_stops_before_globally_unscheduled_candidate(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        version_ids = _seed_claim_version_family(conn, prefix="cursor")
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at, "
            "available_at, completed_at) VALUES "
            "('cursor-job','ingest','{}','completed',0,?,?,?,?)",
            (
                "2019-01-01T00:00:00+00:00",
                "2019-01-01T00:00:00+00:00",
                "2019-01-01T00:00:00+00:00",
                "2019-01-01T00:00:00+00:00",
            ),
        )

    plan = governance_store.plan_history_retention(
        conn,
        cutoff="2026-08-01T00:00:00+00:00",
        plan_as_of=RECENT,
        batch_size=1,
        keep_change_sets=0,
        keep_terminal_jobs=0,
        keep_terminal_outbox=0,
        keep_versions_per_family=1,
    )

    assert plan["selected_ids"]["jobs"] == ["cursor-job"]
    assert plan["selected_ids"]["claim_versions"] == []
    claim_scan = plan["version_scan"]["claim_versions"]
    assert claim_scan["eligible_rows"] == 1
    assert claim_scan["scheduled_rows"] == 0
    assert claim_scan["safe_next_cursor"] == version_ids[0]
    assert conn.execute(
        "SELECT COUNT(*) FROM history_retention_runs_v6"
    ).fetchone()[0] == 0

    with db_store.transaction():
        governance_store.apply_history_retention_plan(
            conn,
            plan,
            confirmation=plan["fingerprint"],
            plan_as_of=plan["plan_as_of"],
        )
    receipt = json.loads(
        conn.execute(
            "SELECT receipt_json FROM history_retention_runs_v6 "
            "WHERE fingerprint = ?",
            (plan["fingerprint"],),
        ).fetchone()[0]
    )
    assert receipt["version_resume_cursors"]["claim_versions"] == version_ids[0]
    with pytest.raises(ValueError, match="prior receipt fingerprint"):
        governance_store.plan_history_retention(
            conn,
            cutoff="2026-08-01T00:00:00+00:00",
            plan_as_of=RECENT,
            claim_version_cursor=version_ids[0],
        )
    next_plan = governance_store.plan_history_retention(
        conn,
        cutoff="2026-08-01T00:00:00+00:00",
        plan_as_of=RECENT,
        batch_size=1,
        keep_change_sets=0,
        keep_terminal_jobs=0,
        keep_terminal_outbox=0,
        keep_versions_per_family=1,
        claim_version_cursor=receipt["version_resume_cursors"]["claim_versions"],
        evidence_version_cursor=receipt["version_resume_cursors"][
            "evidence_versions"
        ],
        version_cursor_receipt=plan["fingerprint"],
    )
    assert (
        next_plan["version_scan"]["claim_versions"]["input_cursor"]
        == version_ids[0]
    )
    with pytest.raises(RuntimeError, match="cursor policy"):
        governance_store.plan_history_retention(
            conn,
            cutoff="2026-08-02T00:00:00+00:00",
            plan_as_of=RECENT,
            batch_size=1,
            keep_change_sets=0,
            keep_terminal_jobs=0,
            keep_terminal_outbox=0,
            keep_versions_per_family=1,
            claim_version_cursor=receipt["version_resume_cursors"][
                "claim_versions"
            ],
            evidence_version_cursor=receipt["version_resume_cursors"][
                "evidence_versions"
            ],
            version_cursor_receipt=plan["fingerprint"],
        )


def test_version_retention_terminal_cursor_is_stable_and_does_not_wrap(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        version_ids = _seed_claim_version_family(conn, prefix="terminal-cursor")

    plan = governance_store.plan_history_retention(
        conn,
        cutoff="2026-08-01T00:00:00+00:00",
        plan_as_of=RECENT,
        batch_size=10,
        keep_change_sets=0,
        keep_terminal_jobs=0,
        keep_terminal_outbox=0,
        keep_versions_per_family=1,
    )
    claim_scan = plan["version_scan"]["claim_versions"]
    assert claim_scan["scan_truncated"] is False
    assert claim_scan["safe_next_cursor"] == version_ids[-1]

    with db_store.transaction():
        governance_store.apply_history_retention_plan(
            conn,
            plan,
            confirmation=plan["fingerprint"],
            plan_as_of=plan["plan_as_of"],
        )
    receipt = json.loads(
        conn.execute(
            "SELECT receipt_json FROM history_retention_runs_v6 "
            "WHERE fingerprint = ?",
            (plan["fingerprint"],),
        ).fetchone()[0]
    )
    terminal_cursor = receipt["version_resume_cursors"]["claim_versions"]
    assert terminal_cursor == version_ids[-1]

    next_plan = governance_store.plan_history_retention(
        conn,
        cutoff="2026-08-01T00:00:00+00:00",
        plan_as_of=RECENT,
        batch_size=10,
        keep_change_sets=0,
        keep_terminal_jobs=0,
        keep_terminal_outbox=0,
        keep_versions_per_family=1,
        claim_version_cursor=terminal_cursor,
        evidence_version_cursor=receipt["version_resume_cursors"][
            "evidence_versions"
        ],
        version_cursor_receipt=plan["fingerprint"],
    )
    next_claim_scan = next_plan["version_scan"]["claim_versions"]
    assert next_claim_scan["input_cursor"] == terminal_cursor
    assert next_claim_scan["scanned_rows"] == 0
    assert next_claim_scan["scan_truncated"] is False
    assert next_claim_scan["safe_next_cursor"] == terminal_cursor
    assert next_plan["selected_count_total"] == 0


def test_version_retention_multi_window_cursors_reach_stable_terminal_keys(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    conn = db_store.get_connection()
    monkeypatch.setattr(governance_store, "_HISTORY_VERSION_MIN_SCAN_ROWS", 2)
    monkeypatch.setattr(governance_store, "_HISTORY_VERSION_MAX_SCAN_ROWS", 2)
    with db_store.transaction():
        for table_name, key_name, id_name, family_name, count in (
            (
                "claim_versions",
                "claim_version_id",
                "claim_id",
                "claim_family_id",
                5,
            ),
            (
                "evidence_versions",
                "evidence_version_id",
                "evidence_id",
                "evidence_family_id",
                3,
            ),
        ):
            prefix = table_name.removesuffix("s")
            for index in range(1, count + 1):
                version_id = f"{prefix}-{index:02d}"
                record_id = f"{id_name}-{index:02d}"
                family_id = f"{family_name}-{index:02d}"
                conn.execute(
                    f"INSERT INTO {table_name} "
                    f"({key_name}, {id_name}, {family_name}, page_key, version_no, "
                    "record_hash, data_json, recorded_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, '{}', ?)",
                    (
                        version_id,
                        record_id,
                        family_id,
                        f"Concept_{version_id}",
                        f"hash-{version_id}",
                        RECENT,
                    ),
                )

    cursors = {"claim_versions": "", "evidence_versions": ""}
    receipt_fingerprint = ""
    observed = []
    for _ in range(10):
        plan = governance_store.plan_history_retention(
            conn,
            cutoff="2026-08-01T00:00:00+00:00",
            plan_as_of=RECENT,
            batch_size=1,
            keep_change_sets=0,
            keep_terminal_jobs=0,
            keep_terminal_outbox=0,
            keep_versions_per_family=1,
            claim_version_cursor=cursors["claim_versions"],
            evidence_version_cursor=cursors["evidence_versions"],
            version_cursor_receipt=receipt_fingerprint,
        )
        next_cursors = {
            table_name: plan["version_scan"][table_name]["safe_next_cursor"]
            for table_name in ("claim_versions", "evidence_versions")
        }
        observed.append(next_cursors)
        assert next_cursors["claim_versions"] >= cursors["claim_versions"]
        assert next_cursors["evidence_versions"] >= cursors["evidence_versions"]
        if next_cursors == cursors:
            break
        with db_store.transaction():
            governance_store.apply_history_retention_plan(
                conn,
                plan,
                confirmation=plan["fingerprint"],
                plan_as_of=plan["plan_as_of"],
            )
        receipt_fingerprint = plan["fingerprint"]
        cursors = next_cursors
    else:
        pytest.fail("version retention cursors did not reach stable terminal keys")

    assert observed == [
        {
            "claim_versions": "claim_version-02",
            "evidence_versions": "evidence_version-02",
        },
        {
            "claim_versions": "claim_version-04",
            "evidence_versions": "evidence_version-03",
        },
        {
            "claim_versions": "claim_version-05",
            "evidence_versions": "evidence_version-03",
        },
        {
            "claim_versions": "claim_version-05",
            "evidence_versions": "evidence_version-03",
        },
    ]
    assert plan["selected_count_total"] == 0
    assert all(
        plan["version_scan"][table_name]["scan_truncated"] is False
        for table_name in ("claim_versions", "evidence_versions")
    )


@pytest.mark.parametrize(
    "version_id",
    ["claim-version-\x00-invalid", "x" * 1025],
)
def test_version_retention_rejects_unresumable_version_id_before_apply(
    isolated_memory,
    version_id,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claim_versions "
            "(claim_version_id, claim_id, claim_family_id, page_key, version_no, "
            "record_hash, data_json, recorded_at) "
            "VALUES (?, 'claim-invalid', 'family-invalid', 'Concept_Invalid', "
            "1, 'hash-invalid', '{}', ?)",
            (version_id, RECENT),
        )

    with pytest.raises(RuntimeError, match="unresumable version identifier"):
        governance_store.plan_history_retention(
            conn,
            cutoff="2026-08-01T00:00:00+00:00",
            plan_as_of=RECENT,
            batch_size=10,
            keep_change_sets=0,
            keep_terminal_jobs=0,
            keep_terminal_outbox=0,
            keep_versions_per_family=1,
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM history_retention_runs_v6"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "table_name,version_field,id_field,family_field,version_id",
    [
        (
            "claim_versions",
            "claim_version_id",
            "claim_id",
            "claim_family_id",
            "",
        ),
        (
            "claim_versions",
            "claim_version_id",
            "claim_id",
            "claim_family_id",
            None,
        ),
        (
            "evidence_versions",
            "evidence_version_id",
            "evidence_id",
            "evidence_family_id",
            "",
        ),
        (
            "evidence_versions",
            "evidence_version_id",
            "evidence_id",
            "evidence_family_id",
            None,
        ),
    ],
)
def test_version_retention_rejects_ids_below_initial_keyset_cursor(
    isolated_memory,
    table_name,
    version_field,
    id_field,
    family_field,
    version_id,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            f"INSERT INTO {table_name} "
            f"({version_field}, {id_field}, {family_field}, page_key, version_no, "
            "record_hash, data_json, recorded_at) "
            "VALUES (?, 'entity-invalid', 'family-invalid', 'Concept_Invalid', "
            "1, 'hash-invalid', '{}', ?)",
            (version_id, RECENT),
        )

    with pytest.raises(RuntimeError, match="unresumable version identifier"):
        governance_store.plan_history_retention(
            conn,
            cutoff="2026-08-01T00:00:00+00:00",
            plan_as_of=RECENT,
            batch_size=10,
            keep_change_sets=0,
            keep_terminal_jobs=0,
            keep_terminal_outbox=0,
            keep_versions_per_family=1,
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM history_retention_runs_v6"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "table_name,version_field,id_field,family_field",
    [
        (
            "claim_versions",
            "claim_version_id",
            "claim_id",
            "claim_family_id",
        ),
        (
            "evidence_versions",
            "evidence_version_id",
            "evidence_id",
            "evidence_family_id",
        ),
    ],
)
@pytest.mark.parametrize("blob_value", [b"", b"abc"])
def test_version_retention_rejects_blob_storage_class_before_apply(
    isolated_memory,
    table_name,
    version_field,
    id_field,
    family_field,
    blob_value,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            f"INSERT INTO {table_name} "
            f"({version_field}, {id_field}, {family_field}, page_key, version_no, "
            "record_hash, data_json, recorded_at) "
            "VALUES (?, 'entity-blob', 'family-blob', 'Concept_Blob', "
            "1, 'hash-blob', '{}', ?)",
            (sqlite3.Binary(blob_value), OLD),
        )

    with pytest.raises(RuntimeError, match="unresumable version identifier"):
        _retention_plan(conn)
    assert conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM history_retention_runs_v6"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "table_name,version_field,id_field,family_field",
    [
        (
            "claim_versions",
            "claim_version_id",
            "claim_id",
            "claim_family_id",
        ),
        (
            "evidence_versions",
            "evidence_version_id",
            "evidence_id",
            "evidence_family_id",
        ),
    ],
)
def test_version_retention_blob_text_representation_collision_is_fail_closed(
    isolated_memory,
    table_name,
    version_field,
    id_field,
    family_field,
):
    db_store.init_db()
    conn = db_store.get_connection()
    text_id = "b'abc'"
    with db_store.transaction():
        conn.execute(
            f"INSERT INTO {table_name} "
            f"({version_field}, {id_field}, {family_field}, page_key, version_no, "
            "record_hash, data_json, recorded_at) "
            "VALUES (?, 'entity-text', 'family-text', 'Concept_Text', "
            "1, 'hash-text', '{}', ?)",
            (text_id, RECENT),
        )
        conn.execute(
            f"INSERT INTO {table_name} "
            f"({version_field}, {id_field}, {family_field}, page_key, version_no, "
            "record_hash, data_json, recorded_at) "
            "VALUES (?, 'entity-blob', 'family-blob', 'Concept_Blob', "
            "1, 'hash-blob', '{}', ?)",
            (sqlite3.Binary(b"abc"), OLD),
        )

    with pytest.raises(RuntimeError, match="unresumable version identifier"):
        _retention_plan(conn)
    preserved = conn.execute(
        f"SELECT recorded_at FROM {table_name} WHERE {version_field} = ?",
        (text_id,),
    ).fetchone()
    assert preserved is not None
    assert preserved["recorded_at"] == RECENT
    assert conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM history_retention_runs_v6"
    ).fetchone()[0] == 0


def test_version_retention_deleted_terminal_keys_are_sticky_and_jobs_continue(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        for table_name, key_name, id_name, family_name, prefix in (
            (
                "claim_versions",
                "claim_version_id",
                "claim_id",
                "claim_family_id",
                "claim",
            ),
            (
                "evidence_versions",
                "evidence_version_id",
                "evidence_id",
                "evidence_family_id",
                "evidence",
            ),
        ):
            conn.executemany(
                f"INSERT INTO {table_name} "
                f"({key_name}, {id_name}, {family_name}, page_key, version_no, "
                "record_hash, data_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '{}', ?)",
                [
                    (
                        f"{prefix}-a-keep",
                        f"{prefix}-id",
                        f"{prefix}-family",
                        f"Concept_{prefix}",
                        2,
                        f"hash-{prefix}-keep",
                        OLD,
                    ),
                    (
                        f"{prefix}-z-delete",
                        f"{prefix}-id",
                        f"{prefix}-family",
                        f"Concept_{prefix}",
                        1,
                        f"hash-{prefix}-delete",
                        OLD,
                    ),
                ],
            )
        job_time = "2021-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at, "
            "available_at, completed_at) VALUES "
            "('later-terminal-job','ingest','{}','completed',0,?,?,?,?)",
            (job_time, job_time, job_time, job_time),
        )

    cursors = {"claim_versions": "", "evidence_versions": ""}
    receipt_fingerprint = ""
    selected_tables = []
    for batch_index in range(3):
        plan = governance_store.plan_history_retention(
            conn,
            cutoff="2026-08-01T00:00:00+00:00",
            plan_as_of=RECENT,
            batch_size=1,
            keep_change_sets=0,
            keep_terminal_jobs=0,
            keep_terminal_outbox=0,
            keep_versions_per_family=1,
            claim_version_cursor=cursors["claim_versions"],
            evidence_version_cursor=cursors["evidence_versions"],
            version_cursor_receipt=receipt_fingerprint,
        )
        selected_tables.append(plan["candidates"][0]["table"])
        if batch_index >= 1:
            assert plan["version_scan"]["claim_versions"]["scanned_rows"] == 0
        if batch_index == 2:
            assert plan["version_scan"]["evidence_versions"]["scanned_rows"] == 0
        with db_store.transaction():
            governance_store.apply_history_retention_plan(
                conn,
                plan,
                confirmation=plan["fingerprint"],
                plan_as_of=plan["plan_as_of"],
            )
        receipt = json.loads(
            conn.execute(
                "SELECT receipt_json FROM history_retention_runs_v6 "
                "WHERE fingerprint = ?",
                (plan["fingerprint"],),
            ).fetchone()[0]
        )
        cursors = {
            table_name: receipt["version_resume_cursors"][table_name]
            for table_name in ("claim_versions", "evidence_versions")
        }
        receipt_fingerprint = plan["fingerprint"]
        if batch_index == 0:
            assert cursors["claim_versions"] == "claim-z-delete"
            assert conn.execute(
                "SELECT COUNT(*) FROM claim_versions "
                "WHERE claim_version_id = 'claim-z-delete'"
            ).fetchone()[0] == 0
        if batch_index == 1:
            assert cursors["evidence_versions"] == "evidence-z-delete"

    assert selected_tables == ["claim_versions", "evidence_versions", "jobs"]


def test_terminal_version_cursors_require_receipt_and_unchanged_generations(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _seed_claim_version_family(conn, prefix="receipt-generation")

    plan = _retention_plan(conn)
    with db_store.transaction():
        governance_store.apply_history_retention_plan(
            conn,
            plan,
            confirmation=plan["fingerprint"],
            plan_as_of=plan["plan_as_of"],
        )
    receipt = json.loads(
        conn.execute(
            "SELECT receipt_json FROM history_retention_runs_v6 "
            "WHERE fingerprint = ?",
            (plan["fingerprint"],),
        ).fetchone()[0]
    )
    cursors = receipt["version_resume_cursors"]
    assert cursors["claim_versions"]
    assert receipt["version_cursor_policy"]["cursor_semantics"] == (
        "last-safe-key-v2"
    )
    assert receipt["version_cursor_policy"]["version_id_invariant"] == (
        "storage-class-text-nonempty-no-nul-max1024-utf8-v2"
    )

    with pytest.raises(ValueError, match="prior receipt fingerprint"):
        governance_store.plan_history_retention(
            conn,
            cutoff=plan["cutoff"],
            plan_as_of=plan["plan_as_of"],
            claim_version_cursor=cursors["claim_versions"],
        )
    with pytest.raises(RuntimeError, match="cursors do not match"):
        governance_store.plan_history_retention(
            conn,
            cutoff=plan["cutoff"],
            plan_as_of=plan["plan_as_of"],
            batch_size=100,
            keep_change_sets=0,
            keep_terminal_jobs=0,
            keep_terminal_outbox=0,
            keep_versions_per_family=1,
            claim_version_cursor=cursors["claim_versions"] + "-tampered",
            evidence_version_cursor=cursors["evidence_versions"],
            version_cursor_receipt=plan["fingerprint"],
        )

    exact = governance_store.plan_history_retention(
        conn,
        cutoff=plan["cutoff"],
        plan_as_of=plan["plan_as_of"],
        batch_size=100,
        keep_change_sets=0,
        keep_terminal_jobs=0,
        keep_terminal_outbox=0,
        keep_versions_per_family=1,
        claim_version_cursor=cursors["claim_versions"],
        evidence_version_cursor=cursors["evidence_versions"],
        version_cursor_receipt=plan["fingerprint"],
    )
    assert exact["version_scan"]["claim_versions"]["scanned_rows"] == 0

    with db_store.transaction():
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at) "
            "VALUES ('generation-drift','ingest','{}','queued',0,?,?)",
            (RECENT, RECENT),
        )
    with pytest.raises(RuntimeError, match="runtime generations drifted"):
        governance_store.plan_history_retention(
            conn,
            cutoff=plan["cutoff"],
            plan_as_of=plan["plan_as_of"],
            batch_size=100,
            keep_change_sets=0,
            keep_terminal_jobs=0,
            keep_terminal_outbox=0,
            keep_versions_per_family=1,
            claim_version_cursor=cursors["claim_versions"],
            evidence_version_cursor=cursors["evidence_versions"],
            version_cursor_receipt=plan["fingerprint"],
        )


def test_version_retention_cursor_does_not_cross_oversize_eligible_row(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        version_ids = _seed_claim_version_family(
            conn,
            prefix="oversize-cursor",
            payload_suffix="x" * 1024,
        )

    plan = governance_store.plan_history_retention(
        conn,
        cutoff="2026-08-01T00:00:00+00:00",
        plan_as_of=RECENT,
        batch_size=10,
        max_delete_bytes=128,
        keep_change_sets=0,
        keep_terminal_jobs=0,
        keep_terminal_outbox=0,
        keep_versions_per_family=1,
    )

    assert plan["selected_ids"]["claim_versions"] == []
    assert plan["candidate_skip_counts"]["oversize_candidates"] == 1
    assert (
        plan["version_scan"]["claim_versions"]["safe_next_cursor"]
        == version_ids[0]
    )


def test_version_retention_vm_work_is_bounded_by_scan_limit(monkeypatch):
    monkeypatch.setattr(governance_store, "_HISTORY_VERSION_MIN_SCAN_ROWS", 100)
    monkeypatch.setattr(governance_store, "_HISTORY_VERSION_MAX_SCAN_ROWS", 100)

    def measured_steps(row_count: int) -> tuple[int, dict[str, object]]:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE claim_versions (
                claim_version_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                claim_family_id TEXT NOT NULL,
                page_key TEXT,
                version_no INTEGER NOT NULL,
                record_hash TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE claims (claim_id TEXT PRIMARY KEY, data_json TEXT NOT NULL);
            CREATE INDEX idx_claim_versions_retention_v6
            ON claim_versions(
                claim_family_id,
                version_no DESC,
                recorded_at DESC,
                claim_version_id DESC
            );
            """
        )
        conn.executemany(
            "INSERT INTO claim_versions VALUES (?, ?, ?, 'Page', 1, ?, '{}', ?)",
            (
                (
                    f"version-{index:08d}",
                    f"claim-{index:08d}",
                    f"family-{index:08d}",
                    "0" * 64,
                    OLD,
                )
                for index in range(row_count)
            ),
        )
        protected = {
            "claim_ids": set(),
            "claim_family_ids": set(),
            "evidence_ids": set(),
            "evidence_family_ids": set(),
            "page_keys": set(),
            "block_version_retention": set(),
            "guard_parts": set(),
        }
        steps = 0

        def progress() -> int:
            nonlocal steps
            steps += 1
            return 0

        conn.set_progress_handler(progress, 1)
        _selected, stats, _trace = (
            governance_store._select_version_retention_candidates(
                conn,
                table_name="claim_versions",
                version_id_field="claim_version_id",
                id_field="claim_id",
                family_field="claim_family_id",
                canonical_table="claims",
                cutoff="2026-08-01T00:00:00+00:00",
                batch_size=1,
                keep_per_family=1,
                protected=protected,
            )
        )
        conn.set_progress_handler(None, 0)
        conn.close()
        return steps, stats

    small_steps, small_stats = measured_steps(1_000)
    large_steps, large_stats = measured_steps(10_000)

    assert small_stats["scanned_rows"] == 100
    assert large_stats["scanned_rows"] == 100
    assert small_stats["scan_truncated"] is True
    assert large_stats["scan_truncated"] is True
    assert large_steps <= small_steps + 100


def test_active_job_and_outbox_protection_scans_fail_closed_at_bounds(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    conn = db_store.get_connection()
    monkeypatch.setattr(governance_store, "_HISTORY_ACTIVE_OUTBOX_MAX_ROWS", 2)
    monkeypatch.setattr(governance_store, "_HISTORY_ACTIVE_JOB_MAX_BYTES", 8)
    with db_store.transaction():
        for index in range(3):
            conn.execute(
                "INSERT INTO mutation_outbox "
                "(filename, mutation_type, status, created_at) "
                "VALUES (?, 'update', 'pending', ?)",
                (f"Page-{index}.md", OLD),
            )
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at) "
            "VALUES ('large-active-job','ingest',?,'queued',0,?,?)",
            (json.dumps({"canonical_name": "中" * 20}), OLD, OLD),
        )

    protected = governance_store._history_active_protections(conn)

    assert "active_outbox_row_limit" in protected["block_version_retention"]
    assert "active_job_byte_limit" in protected["block_version_retention"]


def test_active_job_byte_guard_covers_status_before_loading_text(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    conn = db_store.get_connection()
    monkeypatch.setattr(governance_store, "_HISTORY_ACTIVE_JOB_MAX_BYTES", 64)
    with db_store.transaction():
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at) "
            "VALUES ('status-heavy-job','ingest','{}',?,0,?,?)",
            ("queued-" + "x" * 10_000, OLD, OLD),
        )
    monkeypatch.setattr(
        governance_store,
        "_history_json_object",
        lambda *_args, **_kwargs: pytest.fail(
            "oversize active job text was loaded before the byte guard"
        ),
    )

    protected = governance_store._history_active_protections(conn)

    assert "active_job_byte_limit" in protected["block_version_retention"]


def test_active_job_protection_uses_only_stable_job_id(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO jobs "
            "(job_id, task_type, payload, status, retries, created_at, updated_at) "
            "VALUES ('stable-active-job','ingest',?,'queued',0,?,?)",
            (json.dumps({"canonical_name": "Concept_Stable"}), OLD, OLD),
        )

    statements = []
    conn.set_trace_callback(statements.append)
    try:
        protected = governance_store._history_active_protections(conn)
    finally:
        conn.set_trace_callback(None)

    job_statements = [
        statement.casefold()
        for statement in statements
        if " from jobs " in f" {statement.casefold()} "
    ]
    assert job_statements
    assert all("rowid" not in statement for statement in job_statements)
    assert all(
        "rowid" not in part.casefold() for part in protected["guard_parts"]
    )
    assert any(
        part.startswith("job:stable-active-job:")
        for part in protected["guard_parts"]
    )


def test_legacy_compaction_detaches_terminal_and_preserves_pending_payload(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    common = {
        "created_at": OLD,
        "origin": "legacy",
        "affected_ids": ["entity-a"],
        "affected_pages": ["Concept_A.md"],
        "proposed_entities": [
            {"entity_id": "entity-a", "page_key": "Concept_A"}
        ],
        "proposed_claims": [],
        "proposed_evidence": [],
        "proposed_source_updates": [],
        "proposed_source_artifacts": [],
        "proposed_extraction_runs": [],
        "proposed_edges": [],
    }
    with db_store.transaction():
        _insert_change_set(
            conn,
            "compact-terminal",
            {**common, "status": "published", "requires_human_review": False},
            updated_at=OLD,
            terminal_at=OLD,
        )
        _insert_change_set(
            conn,
            "compact-pending",
            {**common, "status": "pending", "requires_human_review": True},
            updated_at=OLD,
            terminal_at=None,
        )
    plan = governance_store.plan_change_set_history_compaction(
        conn,
        max_rows=10,
        max_input_bytes=1024 * 1024,
    )
    assert "storage_rowid" not in json.dumps(plan, sort_keys=True)

    with db_store.transaction():
        result = governance_store.apply_change_set_history_compaction_plan(
            conn,
            plan,
            confirmation=plan["fingerprint"],
        )

    assert result["compacted_rows"] == 2
    manifests = {
        row["change_set_id"]: json.loads(row["data_json"])
        for row in conn.execute(
            "SELECT change_set_id, data_json FROM change_sets"
        ).fetchall()
    }
    assert manifests["compact-terminal"]["payload"]["available"] is False
    assert (
        manifests["compact-terminal"]["payload"]["codec"]
        == governance_store._CHANGE_SET_DETACHED_LEGACY_CODEC
    )
    assert manifests["compact-pending"]["payload"]["available"] is True
    assert all(
        "proposed_entities" not in manifest for manifest in manifests.values()
    )
    assert conn.execute("SELECT COUNT(*) FROM change_set_payloads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM change_set_payload_refs").fetchone()[0] == 1
    hydrated = governance_store._hydrate_change_set(
        manifests["compact-pending"],
        connection=conn,
    )
    assert hydrated["proposed_entities"] == common["proposed_entities"]


def test_legacy_compaction_scan_and_oversize_samples_are_bounded(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    conn = db_store.get_connection()
    monkeypatch.setattr(
        governance_store,
        "_CHANGE_SET_COMPACTION_MAX_SCAN_ROWS",
        25,
    )
    with db_store.transaction():
        for index in range(30):
            _insert_change_set(
                conn,
                f"oversize-{index:02d}",
                {
                    "status": "published",
                    "created_at": OLD,
                    "published_at": OLD,
                    "payload": "中" * 100,
                },
                updated_at=OLD,
                terminal_at=OLD,
            )

    plan = governance_store.plan_change_set_history_compaction(
        conn,
        max_rows=10,
        max_input_bytes=1,
    )

    assert plan["scanned_rows"] == 25
    assert plan["scan_limit"] == 25
    assert plan["scan_truncated"] is True
    assert plan["selected_rows"] == 0
    assert plan["oversize_count"] == 25
    assert len(plan["oversize_samples"]) == 20


@pytest.mark.parametrize(
    "invalid_id",
    [None, "", sqlite3.Binary(b"blob-id")],
    ids=("null", "empty-text", "blob"),
)
def test_legacy_compaction_rejects_unresumable_id_storage_before_plan(
    isolated_memory,
    invalid_id,
):
    db_store.init_db()
    conn = db_store.get_connection()
    raw = json.dumps({"status": "published", "published_at": OLD})
    guard = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
            "VALUES (?, ?, ?)",
            (invalid_id, raw, OLD),
        )
        conn.execute(
            "INSERT INTO change_set_lifecycle_v6 "
            "(change_set_id, status, created_at, terminal_at, time_source, "
            "payload_guard_sha256) VALUES (?, 'published', ?, ?, 'test_seed', ?)",
            (invalid_id, OLD, OLD, guard),
        )

    with pytest.raises(RuntimeError, match="unresumable change-set identifier"):
        governance_store.plan_change_set_history_compaction(conn)
    assert conn.execute("SELECT COUNT(*) FROM change_set_payloads").fetchone()[0] == 0


@pytest.mark.parametrize(
    "invalid_id",
    ["nul\x00id", "x" * 1025],
    ids=("nul", "oversize-utf8"),
)
def test_legacy_compaction_rejects_unresumable_text_id_before_safe_cursor(
    isolated_memory,
    invalid_id,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _insert_change_set(
            conn,
            invalid_id,
            {"status": "published", "created_at": OLD, "published_at": OLD},
            updated_at=OLD,
            terminal_at=OLD,
        )

    with pytest.raises(RuntimeError, match="unresumable change-set identifier"):
        governance_store.plan_change_set_history_compaction(conn)


def test_legacy_compaction_blob_text_representation_collision_fails_closed(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    text_id = "b'abc'"
    text_raw = json.dumps(
        {"change_set_id": text_id, "status": "published", "published_at": OLD},
        separators=(",", ":"),
    )
    blob_raw = json.dumps({"status": "published", "published_at": OLD})
    with db_store.transaction():
        conn.execute(
            "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
            "VALUES (?, ?, ?)",
            (text_id, text_raw, OLD),
        )
        conn.execute(
            "INSERT INTO change_set_lifecycle_v6 "
            "(change_set_id, status, created_at, terminal_at, time_source, "
            "payload_guard_sha256) VALUES (?, 'published', ?, ?, 'test_seed', ?)",
            (
                text_id,
                OLD,
                OLD,
                hashlib.sha256(text_raw.encode("utf-8")).hexdigest(),
            ),
        )
        blob_id = sqlite3.Binary(b"abc")
        conn.execute(
            "INSERT INTO change_sets (change_set_id, data_json, updated_at) "
            "VALUES (?, ?, ?)",
            (blob_id, blob_raw, OLD),
        )
        conn.execute(
            "INSERT INTO change_set_lifecycle_v6 "
            "(change_set_id, status, created_at, terminal_at, time_source, "
            "payload_guard_sha256) VALUES (?, 'published', ?, ?, 'test_seed', ?)",
            (
                blob_id,
                OLD,
                OLD,
                hashlib.sha256(blob_raw.encode("utf-8")).hexdigest(),
            ),
        )

    with pytest.raises(RuntimeError, match="unresumable change-set identifier"):
        governance_store.plan_change_set_history_compaction(conn)
    row = conn.execute(
        "SELECT data_json FROM change_sets WHERE change_set_id = ?", (text_id,)
    ).fetchone()
    assert row["data_json"] == text_raw
    assert conn.execute("SELECT COUNT(*) FROM change_set_payloads").fetchone()[0] == 0


def test_legacy_compaction_apply_rejects_recomputed_policy_drift(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _insert_change_set(
            conn,
            "policy-drift",
            {"status": "published", "created_at": OLD, "published_at": OLD},
            updated_at=OLD,
            terminal_at=OLD,
        )
    plan = governance_store.plan_change_set_history_compaction(conn)
    tampered = dict(plan)
    tampered["cursor_policy"] = {
        **plan["cursor_policy"],
        "change_set_id_invariant": "legacy-v1",
    }
    tampered["cursor_policy_sha256"] = (
        governance_store._change_set_compaction_cursor_policy_sha256(
            tampered["cursor_policy"]
        )
    )
    tampered["fingerprint"] = governance_store._history_plan_fingerprint(tampered)

    with pytest.raises(ValueError, match="cursor policy is invalid"):
        with db_store.transaction():
            governance_store.apply_change_set_history_compaction_plan(
                conn,
                tampered,
                confirmation=tampered["fingerprint"],
            )


def test_legacy_compaction_oversized_first_row_is_metadata_only_and_resumable(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    conn = db_store.get_connection()
    monkeypatch.setattr(
        governance_store,
        "_CHANGE_SET_COMPACTION_MAX_SCAN_ROWS",
        1,
    )
    with db_store.transaction():
        _insert_change_set(
            conn,
            "a-oversized",
            {
                "status": "published",
                "created_at": OLD,
                "published_at": OLD,
                "payload": "x" * 4096,
            },
            updated_at=OLD,
            terminal_at=OLD,
        )
        _insert_change_set(
            conn,
            "z-compactable",
            {
                "status": "published",
                "created_at": OLD,
                "published_at": OLD,
            },
            updated_at=OLD,
            terminal_at=OLD,
        )

    statements = []
    conn.set_trace_callback(statements.append)
    streaming_hash = governance_store._sqlite_text_blob_sha256
    parse_json_object = governance_store._history_json_object
    monkeypatch.setattr(
        governance_store,
        "_sqlite_text_blob_sha256",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized compaction row was hashed before the byte guard"
        ),
    )
    monkeypatch.setattr(
        governance_store,
        "_history_json_object",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized compaction row was parsed before the byte guard"
        ),
    )

    first = governance_store.plan_change_set_history_compaction(
        conn,
        max_rows=1,
        max_input_bytes=512,
    )
    conn.set_trace_callback(None)

    assert first["selected_rows"] == 0
    assert first["oversize_count"] == 1
    assert first["preflight_input_bytes"] == 0
    assert first["scan_truncated"] is True
    assert first["compaction_exhausted"] is False
    assert first["safe_next_cursor"] == "a-oversized"
    scan_sql = next(
        statement
        for statement in statements
        if "FROM change_sets JOIN change_set_lifecycle_v6" in statement
    )
    assert "json_valid" not in scan_sql.lower()
    assert "json_extract" not in scan_sql.lower()
    assert "select change_sets.data_json" not in scan_sql.lower()

    monkeypatch.setattr(
        governance_store,
        "_sqlite_text_blob_sha256",
        streaming_hash,
    )
    monkeypatch.setattr(
        governance_store,
        "_history_json_object",
        parse_json_object,
    )
    second = governance_store.plan_change_set_history_compaction(
        conn,
        max_rows=1,
        max_input_bytes=512,
        cursor=first["safe_next_cursor"],
    )

    assert [row["change_set_id"] for row in second["candidates"]] == [
        "z-compactable"
    ]


def test_legacy_compaction_receipt_cursor_crosses_uncompactable_safe_prefix(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    conn = db_store.get_connection()
    monkeypatch.setattr(
        governance_store,
        "_CHANGE_SET_COMPACTION_MAX_SCAN_ROWS",
        1,
    )
    with db_store.transaction():
        _insert_change_set(
            conn,
            "a-uncompactable",
            {
                "status": "pending",
                "created_at": OLD,
                "affected_ids": [f"pending-id-{index:05d}" for index in range(20_001)],
            },
            updated_at=OLD,
            terminal_at=None,
        )
        _insert_change_set(
            conn,
            "z-compactable",
            {
                "status": "published",
                "created_at": OLD,
                "published_at": OLD,
            },
            updated_at=OLD,
            terminal_at=OLD,
        )

    first = governance_store.plan_change_set_history_compaction(
        conn,
        max_rows=1,
        max_input_bytes=2 * 1024 * 1024,
    )

    assert first["selected_rows"] == 0
    assert first["uncompactable_count"] == 1
    assert first["scan_truncated"] is True
    assert first["compaction_exhausted"] is False
    assert first["safe_next_cursor"] == "a-uncompactable"
    tampered = dict(first)
    tampered["safe_next_cursor"] = "z-compactable"
    with pytest.raises(RuntimeError, match="fingerprint is invalid"):
        with db_store.transaction():
            governance_store.apply_change_set_history_compaction_plan(
                conn,
                tampered,
                confirmation=first["fingerprint"],
            )
    with db_store.transaction():
        receipt = governance_store.apply_change_set_history_compaction_plan(
            conn,
            first,
            confirmation=first["fingerprint"],
        )
    assert receipt["safe_next_cursor"] == first["safe_next_cursor"]

    second = governance_store.plan_change_set_history_compaction(
        conn,
        max_rows=1,
        max_input_bytes=2 * 1024 * 1024,
        cursor=receipt["safe_next_cursor"],
    )

    assert second["input_cursor"] == "a-uncompactable"
    assert [row["change_set_id"] for row in second["candidates"]] == [
        "z-compactable"
    ]


def test_public_compaction_cursor_crosses_uncompactable_scan_window(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    conn = db_store.get_connection()
    monkeypatch.setattr(
        governance_store,
        "_CHANGE_SET_COMPACTION_MAX_SCAN_ROWS",
        1,
    )
    with db_store.transaction():
        _insert_change_set(
            conn,
            "a-uncompactable-public",
            {
                "status": "pending",
                "created_at": OLD,
                "affected_ids": [
                    f"pending-id-{index:05d}" for index in range(20_001)
                ],
            },
            updated_at=OLD,
            terminal_at=None,
        )
        _insert_change_set(
            conn,
            "z-compactable-public",
            {
                "status": "published",
                "created_at": OLD,
                "published_at": OLD,
            },
            updated_at=OLD,
            terminal_at=OLD,
        )

    first_preview = json.loads(
        maintenance.compact_change_set_history(
            max_rows=1,
            max_input_bytes=2 * 1024 * 1024,
        )
    )
    assert first_preview["input_cursor"] == ""
    assert first_preview["selected_rows"] == 0
    assert first_preview["safe_next_cursor"] == "a-uncompactable-public"

    first_apply = json.loads(
        maintenance.compact_change_set_history(
            dry_run=False,
            max_rows=1,
            max_input_bytes=2 * 1024 * 1024,
            confirmation=first_preview["fingerprint"],
        )
    )
    issued_cursor = first_apply["result"]["safe_next_cursor"]
    assert issued_cursor == first_preview["safe_next_cursor"]

    second_preview = json.loads(
        maintenance.compact_change_set_history(
            max_rows=1,
            max_input_bytes=2 * 1024 * 1024,
            cursor=issued_cursor,
        )
    )
    assert second_preview["input_cursor"] == issued_cursor
    assert second_preview["selected_rows"] == 1
    assert second_preview["selected_samples"] == [
        {
            "change_set_id": "z-compactable-public",
            "input_bytes": second_preview["selected_input_bytes"],
            "status": "published",
        }
    ]
    second_apply = json.loads(
        maintenance.compact_change_set_history(
            dry_run=False,
            max_rows=1,
            max_input_bytes=2 * 1024 * 1024,
            confirmation=second_preview["fingerprint"],
            cursor=issued_cursor,
        )
    )
    assert second_apply["result"]["compacted_rows"] == 1
    compacted = json.loads(
        conn.execute(
            "SELECT data_json FROM change_sets "
            "WHERE change_set_id = 'z-compactable-public'"
        ).fetchone()[0]
    )
    assert compacted["manifest_version"] == 2


def test_terminal_compaction_does_not_rebuild_or_compress_snapshot_payload(
    isolated_memory,
    monkeypatch,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        raw = _insert_change_set(
            conn,
            "terminal-detached-stream",
            {
                "status": "published",
                "created_at": OLD,
                "published_at": OLD,
                "affected_pages": ["Concept_Stream.md"],
                "affected_ids": ["entity-stream"],
                "proposed_entities": [{"body": "x" * (1024 * 1024)}],
            },
            updated_at=OLD,
            terminal_at=OLD,
        )
    plan = governance_store.plan_change_set_history_compaction(
        conn,
        max_rows=1,
        max_input_bytes=2 * 1024 * 1024,
    )
    monkeypatch.setattr(
        governance_store,
        "_canonical_change_set_payload",
        lambda *_args, **_kwargs: pytest.fail("terminal payload was rebuilt"),
    )

    with db_store.transaction():
        governance_store.apply_change_set_history_compaction_plan(
            conn,
            plan,
            confirmation=plan["fingerprint"],
        )

    manifest = json.loads(
        conn.execute(
            "SELECT data_json FROM change_sets "
            "WHERE change_set_id = 'terminal-detached-stream'"
        ).fetchone()[0]
    )
    assert manifest["payload"]["codec"] == governance_store._CHANGE_SET_DETACHED_LEGACY_CODEC
    assert manifest["payload"]["raw_bytes"] == len(raw.encode("utf-8"))
    assert manifest["payload"]["stored_bytes"] == 0


def test_terminal_compaction_accepts_historical_affected_id_cardinality(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    affected_ids = [f"historical-id-{index:05d}" for index in range(47_098)]
    with db_store.transaction():
        _insert_change_set(
            conn,
            "terminal-many-affected-ids",
            {
                "status": "published",
                "created_at": OLD,
                "published_at": OLD,
                "affected_pages": ["Concept_Historical-Ids.md"],
                "affected_ids": affected_ids,
            },
            updated_at=OLD,
            terminal_at=OLD,
        )

    plan = governance_store.plan_change_set_history_compaction(
        conn,
        max_rows=1,
        max_input_bytes=4 * 1024 * 1024,
    )

    assert plan["selected_rows"] == 1
    assert plan["uncompactable_count"] == 0
    with db_store.transaction():
        result = governance_store.apply_change_set_history_compaction_plan(
            conn,
            plan,
            confirmation=plan["fingerprint"],
        )

    assert result["compacted_rows"] == 1
    manifest = json.loads(
        conn.execute(
            "SELECT data_json FROM change_sets "
            "WHERE change_set_id = 'terminal-many-affected-ids'"
        ).fetchone()[0]
    )
    expected_ids = sorted(set(affected_ids))
    assert manifest["affected_id_count"] == 47_098
    assert len(manifest["affected_ids"]) == governance_store._CHANGE_SET_ID_PREVIEW_LIMIT
    assert manifest["affected_ids_sha256"] == hashlib.sha256(
        "\n".join(expected_ids).encode("utf-8")
    ).hexdigest()


def test_compaction_preview_classifies_uncompactable_pending_legacy(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _insert_change_set(
            conn,
            "pending-too-many-affected-ids",
            {
                "status": "pending",
                "created_at": OLD,
                "affected_pages": ["Concept_Pending-Legacy.md"],
                "affected_ids": [f"pending-id-{index:05d}" for index in range(20_001)],
            },
            updated_at=OLD,
            terminal_at=None,
        )

    plan = governance_store.plan_change_set_history_compaction(
        conn,
        max_rows=1,
        max_input_bytes=2 * 1024 * 1024,
    )

    assert plan["selected_rows"] == 0
    assert plan["uncompactable_count"] == 1
    assert plan["uncompactable_samples"] == [
        {
            "change_set_id": "pending-too-many-affected-ids",
            "input_bytes": plan["preflight_input_bytes"],
            "reason": "ChangeSetPayloadTooLarge",
            "detail": (
                "Change-set affected_ids exceeds hard limit: 20001 > 20000"
            ),
        }
    ]
    with db_store.transaction():
        result = governance_store.apply_change_set_history_compaction_plan(
            conn,
            plan,
            confirmation=plan["fingerprint"],
        )
    assert result["compacted_rows"] == 0


def test_retention_apply_rejects_forged_oversize_batch(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    plan = _retention_plan(conn)
    plan["rules"]["max_delete_bytes"] = (
        governance_store._HISTORY_RETENTION_MAX_DELETE_BYTES + 1
    )
    plan["fingerprint"] = governance_store._history_plan_fingerprint(plan)

    with pytest.raises(ValueError, match="byte bound"):
        with db_store.transaction():
            governance_store.apply_history_retention_plan(
                conn,
                plan,
                confirmation=plan["fingerprint"],
                plan_as_of=plan["plan_as_of"],
            )
