"""D5, first table: ``entities.page_key`` becomes a column, and the json indexes go.

The frozen template (PROPOSAL_2026-09-18.md) is a *virtual generated column*, not a projection
table with triggers: it is by construction always equal to the json it derives from, so there is no
drift to compare and nothing to keep in step.  The migration is an idempotent ``ALTER`` guarded by
``PRAGMA table_xinfo`` -- ``table_info`` omits generated columns, which is exactly how a probe built
on it reports the column missing forever and re-runs the ``ALTER`` until it fails.

These tests pin the two halves that can break silently: that the column answers what the json
answered, and that a database created before the column existed converges when ``init_db`` runs.
"""

import sqlite3

from vector_lake import db_store


def _entity(conn, entity_id: str, page_key: str, *, kind="concept", status="Active"):
    conn.execute(
        "INSERT OR REPLACE INTO entities (entity_id, canonical_name, data_json, updated_at, type, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            entity_id,
            page_key,
            '{"page_key": "%s", "type": "%s", "status": "%s"}' % (page_key, kind, status),
            "2026-01-01T00:00:00Z",
            kind,
            status,
        ),
    )


def test_the_generated_column_answers_exactly_what_the_json_answered(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    for index, key in enumerate(("Concept_One", "Vendor_Two", "Person_三")):
        _entity(conn, f"e{index}", key)

    rows = conn.execute(
        "SELECT entity_id, f_page_key, json_extract(data_json, '$.page_key') FROM entities ORDER BY entity_id"
    ).fetchall()

    assert rows, "the fixture inserted nothing"
    for entity_id, generated, from_json in rows:
        assert generated == from_json, (entity_id, generated, from_json)

    # The rewritten query shape works, and the plan uses the index built on the column.
    assert conn.execute(
        "SELECT entity_id FROM entities WHERE f_page_key = ?", ("Vendor_Two",)
    ).fetchone()[0] == "e1"
    plan = " ".join(
        str(row[3]) for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT entity_id FROM entities WHERE f_page_key = ?", ("Vendor_Two",)
        )
    )
    assert "idx_entities_f_page_key" in plan, plan


def test_a_database_without_the_column_converges(isolated_memory):
    """The fast path must notice, or the rewritten queries ask for a column that is not there.

    This is the failure the suite caught while the batch was being built: the sentinels were all
    present, so ``init_db`` skipped the DDL and never ran the ``ALTER``.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    db_path = db_store.get_db_path()

    # A pre-migration database: the column is gone, every sentinel is present.
    # The index on it has to go first: SQLite refuses to drop a column an index uses.
    conn.execute("DROP INDEX IF EXISTS idx_entities_f_page_key")
    conn.execute("ALTER TABLE entities DROP COLUMN f_page_key")
    conn.commit()
    assert "f_page_key" not in {
        str(row[1]) for row in conn.execute("PRAGMA table_xinfo(entities)")
    }
    assert db_store._entities_format_is_stale(conn) is True

    db_store._INITIALIZED_DB_PATHS.discard(str(db_path.resolve()))
    db_store.init_db()

    assert "f_page_key" in {
        str(row[1]) for row in conn.execute("PRAGMA table_xinfo(entities)")
    }, "init_db took the fast path and left the column missing"
    assert db_store._entities_format_is_stale(conn) is False
    # Idempotent: a second pass must not attempt the ALTER again.
    db_store._INITIALIZED_DB_PATHS.discard(str(db_path.resolve()))
    db_store.init_db()
    assert "f_page_key" in {
        str(row[1]) for row in conn.execute("PRAGMA table_xinfo(entities)")
    }


def test_the_three_json_indexes_are_dropped_by_their_prune(isolated_memory):
    """They are what the column replaces, so a database that still has them converges."""
    db_store.init_db()
    conn = db_store.get_connection()
    for name, target in (
        ("idx_entities_type", "json_extract(data_json, '$.type')"),
        ("idx_entities_status", "json_extract(data_json, '$.status')"),
        ("idx_entities_page_key", "json_extract(data_json, '$.page_key')"),
    ):
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON entities ({target})")
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-entities-json-indexes'")
    conn.commit()
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert {"idx_entities_type", "idx_entities_status", "idx_entities_page_key"} <= names

    applied = db_store.apply_legacy_schema_prunes()

    assert "2026-09-18-entities-json-indexes" in applied
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert not {"idx_entities_type", "idx_entities_status", "idx_entities_page_key"} & names
    # And the DDL no longer creates them, which the check above cannot show: the ledger has now
    # recorded the prune, so a DDL that still created them would never be corrected again.  Re-run
    # the DDL on this database with the prune forgotten and watch what it creates.
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-entities-json-indexes'")
    conn.commit()
    db_store._init_db_once(str(db_store.get_db_path().resolve()))
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert not {"idx_entities_type", "idx_entities_status", "idx_entities_page_key"} & names, (
        "the schema DDL still creates the json indexes"
    )
    assert "idx_entities_f_page_key" in names


def test_the_leftover_type_status_index_is_dropped_under_its_own_name(isolated_memory):
    """The archived migration created it; a recorded migration would skip an added step."""
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type_status ON entities(type, status)")
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-entities-type-status-index'")
    conn.commit()
    assert "idx_entities_type_status" in {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master")}

    applied = db_store.apply_legacy_schema_prunes()

    assert "2026-09-18-entities-type-status-index" in applied
    assert "idx_entities_type_status" not in {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master")}


def test_the_health_gate_converges_a_pre_migration_database(isolated_memory):
    """The write gate runs before the coordinator's ``init_db``, so it must not need the column.

    It used to call ``init_db`` only when the database file was missing, which meant a database that
    exists without the column raised ``no such column: f_page_key`` here -- the gate failing before
    the code that would have fixed the schema.
    """
    from vector_lake import runtime_health

    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("DROP INDEX IF EXISTS idx_entities_f_page_key")
    conn.execute("ALTER TABLE entities DROP COLUMN f_page_key")
    conn.commit()
    db_store._INITIALIZED_DB_PATHS.clear()

    assessment = runtime_health.assess_runtime_health()

    assert assessment["ok"] is True, assessment["issues"]
    assert "f_page_key" in {
        str(row[1]) for row in conn.execute("PRAGMA table_xinfo(entities)")
    }, "the gate reported healthy without converging the schema"
