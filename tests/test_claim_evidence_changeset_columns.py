"""D5, remaining tables: the claim, evidence and change-set queries name columns.

Same template, and the rule that made the last table cheap applies again: classify before adding.
``claims.status`` is a real column and nothing filters the json spelling, so it is not touched.  The
other paths are json-only and queried, so they get generated columns -- and because the expression
indexes were still serving some of those queries *verbatim*, every consumer had to move in the same
commit or the index would have been replaced by one nothing could use.
"""

import json

from vector_lake import db_store


def _insert(conn, table, key_column, key, payload, *, status=None):
    columns = [key_column, "data_json", "updated_at"]
    values = [key, json.dumps(payload), "2026-01-01T00:00:00Z"]
    if status is not None:
        columns.insert(2, "status")
        values.insert(2, status)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", values)


def test_the_generated_columns_equal_their_json_on_every_table(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _insert(conn, "claims", "claim_id", "c1",
            {"claim_type": "timeline-event", "locator": {"page_key": "Concept_A"}, "source_page": "Concept_A.md"},
            status="Active")
    _insert(conn, "evidence", "evidence_id", "v1",
            {"locator": {"page_key": "Concept_A"}, "claim_id": "c1"})
    _insert(conn, "change_sets", "change_set_id", "s1",
            {"status": "pending", "idempotency_key": "idem-1"})

    for table, columns in (
        ("claims", [("f_claim_type", "$.claim_type"), ("f_page_key", "$.locator.page_key"), ("f_source_page", "$.source_page")]),
        ("evidence", [("f_page_key", "$.locator.page_key")]),
        ("change_sets", [("f_status", "$.status"), ("f_idempotency_key", "$.idempotency_key")]),
    ):
        key = {"claims": "claim_id", "evidence": "evidence_id", "change_sets": "change_set_id"}[table]
        for column, path in columns:
            rows = conn.execute(
                f"SELECT {key} AS k, {column} AS col, json_extract(data_json, '{path}') AS js FROM {table}"
            ).fetchall()
            assert rows, (table, column)
            for row in rows:
                assert row["col"] == row["js"], (table, column, dict(row))


def test_the_rewritten_query_shapes_work_and_use_their_indexes(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _insert(conn, "claims", "claim_id", "c1",
            {"claim_type": "timeline-event", "locator": {"page_key": "Concept_A"}, "source_page": "Concept_A.md"})

    for sql, params, expected in (
        ("SELECT claim_id FROM claims WHERE f_claim_type = ?", ("timeline-event",), "idx_claims_f_claim_type"),
        ("SELECT claim_id FROM claims WHERE f_page_key = ?", ("Concept_A",), "idx_claims_f_page_key"),
        ("SELECT claim_id FROM claims WHERE f_source_page IN (?, ?)", ("Concept_A", "Concept_A.md"), "idx_claims_f_source_page"),
    ):
        plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
        assert expected in plan, (sql, plan)


def test_a_database_without_the_columns_converges(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    db_path = db_store.get_db_path()
    for table, column, index in (
        ("claims", "f_claim_type", "idx_claims_f_claim_type"),
        ("claims", "f_page_key", "idx_claims_f_page_key"),
        ("claims", "f_source_page", "idx_claims_f_source_page"),
        ("evidence", "f_page_key", "idx_evidence_f_page_key"),
        ("change_sets", "f_status", "idx_change_sets_f_status"),
        ("change_sets", "f_idempotency_key", "idx_change_sets_f_idempotency"),
    ):
        conn.execute(f"DROP INDEX IF EXISTS {index}")
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.commit()
    assert db_store._claims_format_is_stale(conn) is True

    db_store._INITIALIZED_DB_PATHS.discard(str(db_path.resolve()))
    db_store.init_db()

    assert db_store._claims_format_is_stale(conn) is False
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert {"idx_claims_f_claim_type", "idx_claims_f_page_key", "idx_evidence_f_page_key"} <= names


def test_the_expression_indexes_go_and_the_ddl_does_not_put_them_back(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    for name, target in (
        ("idx_claims_page_key", "claims(json_extract(data_json, '$.locator.page_key'))"),
        ("idx_claims_claim_type", "claims(json_extract(data_json, '$.claim_type'))"),
        ("idx_evidence_page_key", "evidence(json_extract(data_json, '$.locator.page_key'))"),
    ):
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-claim-evidence-json-indexes'")
    conn.commit()

    applied = db_store.apply_legacy_schema_prunes()

    assert "2026-09-18-claim-evidence-json-indexes" in applied
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert not {"idx_claims_page_key", "idx_claims_claim_type", "idx_evidence_page_key"} & names

    # And the DDL must not re-create them, or a migrated database drifts on the next start.
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-claim-evidence-json-indexes'")
    conn.commit()
    db_store._init_db_once(str(db_store.get_db_path().resolve()))
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert not {"idx_claims_page_key", "idx_claims_claim_type", "idx_evidence_page_key"} & names


def test_the_unreachable_governance_queue_index_is_dropped(isolated_memory):
    """No statement filters that table at all, so the expression index cannot be reached.

    It is read through the generic ``_load_db_queue`` helper, which selects every row and filters
    nothing; ``$.change_set_id`` is not even present in the live rows.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_governance_queue_change_set_status "
        "ON governance_queue(CASE WHEN json_valid(data_json) THEN json_extract(data_json, '$.change_set_id') END)"
    )
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-governance-queue-index'")
    conn.commit()
    assert "idx_governance_queue_change_set_status" in {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master")}

    applied = db_store.apply_legacy_schema_prunes()

    assert "2026-09-18-governance-queue-index" in applied
    assert "idx_governance_queue_change_set_status" not in {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master")}


def test_a_missing_replacement_index_is_reported_as_stale(isolated_memory):
    """The DDL's index block is one try/except; a partial failure must not be permanent.

    The fast path never re-runs that DDL, so without the index in the probe a failure there would
    skip the rest of the block, leave the retired expression index in place (the prune keeps it when
    the replacement is absent), and cost a full scan on every claim lookup -- silently and forever.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    assert db_store._claims_format_is_stale(conn) is False

    conn.execute("DROP INDEX idx_claims_f_claim_type")
    conn.commit()

    assert db_store._claims_format_is_stale(conn) is True, (
        "a missing replacement index must send init_db back down the DDL path"
    )
    db_store._INITIALIZED_DB_PATHS.clear()
    db_store.init_db()
    assert db_store._claims_format_is_stale(conn) is False
    assert "idx_claims_f_claim_type" in {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }


def test_the_prune_keeps_an_old_index_when_its_replacement_is_absent(isolated_memory):
    """The conditional branch the docstring relies on, pinned so it cannot be simplified away."""
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_page_key ON claims(json_extract(data_json, '$.locator.page_key'))")
    conn.execute("DROP INDEX idx_claims_f_page_key")
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-claim-evidence-json-indexes'")
    conn.commit()

    applied = db_store.apply_legacy_schema_prunes()

    assert "2026-09-18-claim-evidence-json-indexes" in applied
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "idx_claims_page_key" in names, (
        "the old index was dropped although its replacement does not exist"
    )
