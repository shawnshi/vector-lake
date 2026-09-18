"""D5, second table: the memory queries name columns, not json paths.

The template's rule 1 is why this table looks different from the first.  ``memory_type`` and
``status`` are already real columns *and* every statement reads them as columns, so the two json
indexes over the same fields were unreachable rather than unused -- an expression index cannot serve
a bare-column predicate -- and they are dropped instead of joined by a third spelling.  ``memory_key``
and ``source_claim_id`` have no real column and are queried, so they get generated columns.
"""

from vector_lake import db_store


def _memory(conn, memory_id: str, *, kind="fact", status="Active", key=None, claim=None):
    import json

    payload = {"memory_type": kind, "status": status, "validity_state": "active"}
    if key is not None:
        payload["memory_key"] = key
    payload["source_claim_id"] = claim
    conn.execute(
        "INSERT OR REPLACE INTO operational_memory "
        "(memory_id, memory_type, score, status, ttl, data_json, updated_at) VALUES (?,?,?,?,?,?,?)",
        (memory_id, kind, 0.5, status, 365.0, json.dumps(payload), "2026-01-01T00:00:00Z"),
    )


def test_the_generated_columns_answer_what_the_json_answered(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _memory(conn, "m1", key="k-one", claim="claim_a")
    _memory(conn, "m2", kind="preference", key="k-two", claim="claim_b")

    rows = conn.execute(
        "SELECT memory_id, f_memory_key, json_extract(data_json,'$.memory_key'), "
        "f_source_claim_id, json_extract(data_json,'$.source_claim_id') FROM operational_memory ORDER BY memory_id"
    ).fetchall()

    assert rows, "the fixture inserted nothing"
    for memory_id, key_col, key_json, claim_col, claim_json in rows:
        assert key_col == key_json, (memory_id, key_col, key_json)
        assert claim_col == claim_json, (memory_id, claim_col, claim_json)

    # The two query shapes the tree issues, and the plans that serve them.
    assert conn.execute(
        "SELECT memory_id FROM operational_memory WHERE memory_type = ? AND f_memory_key = ?",
        ("preference", "k-two"),
    ).fetchone()[0] == "m2"
    # Name the index rather than accepting any plan that mentions one: a substring test for
    # "INDEX" is also satisfied by a covering scan of the primary-key autoindex.
    for sql, params, expected in (
        ("SELECT memory_id FROM operational_memory WHERE memory_type = ? AND f_memory_key = ?",
         ("fact", "k-one"), "idx_om_f_memory_key"),
        ("SELECT memory_id FROM operational_memory WHERE f_source_claim_id = ?",
         ("claim_a",), "idx_om_f_source_claim"),
    ):
        plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
        assert expected in plan, (sql, plan)


def test_a_database_without_the_columns_converges(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    db_path = db_store.get_db_path()
    for column, index in (("f_memory_key", "idx_om_f_memory_key"), ("f_source_claim_id", "idx_om_f_source_claim")):
        conn.execute(f"DROP INDEX IF EXISTS {index}")
        conn.execute(f"ALTER TABLE operational_memory DROP COLUMN {column}")
    conn.commit()
    assert db_store._om_probability_format_is_stale(conn) is True

    db_store._INITIALIZED_DB_PATHS.discard(str(db_path.resolve()))
    db_store.init_db()

    assert db_store._om_probability_format_is_stale(conn) is False
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert {"idx_om_f_memory_key", "idx_om_f_source_claim"} <= names


def test_the_json_indexes_are_dropped_and_not_recreated(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    # Put the pre-change expression indexes back, as a database that predates this batch has them.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_key ON operational_memory (memory_type, json_extract(data_json, '$.memory_key'))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_source_claim ON operational_memory (json_extract(data_json, '$.source_claim_id'))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON operational_memory (json_extract(data_json, '$.memory_type'))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_status ON operational_memory (json_extract(data_json, '$.status'))")
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-om-json-indexes'")
    conn.commit()

    applied = db_store.apply_legacy_schema_prunes()

    assert "2026-09-18-om-json-indexes" in applied
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert not {"idx_memory_key", "idx_memory_source_claim", "idx_memory_type", "idx_memory_status"} & names
    assert {"idx_om_f_memory_key", "idx_om_f_source_claim"} <= names
    # The real-column indexes are not this prune's business.  The reason is *not* that this table is
    # filtered by them -- it is not; the four `memory_type IN (...)` sites filter
    # `operational_memory_index` -- but that removing an index was measured harmful in general and
    # the write-cost question belongs to the search path, not to this migration.
    for name, target in (
        ("idx_om_type", "operational_memory(memory_type)"),
        ("idx_om_status", "operational_memory(status)"),
        ("idx_memory_type_status", "operational_memory(memory_type, status)"),
    ):
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-om-json-indexes'")
    conn.commit()
    db_store.apply_legacy_schema_prunes()
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert {"idx_om_type", "idx_om_status", "idx_memory_type_status"} <= names, (
        "the prune touched a real-column index the search path uses"
    )

    # The DDL must not put the retired ones back, or a migrated database drifts on the next start.
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-om-json-indexes'")
    conn.commit()
    db_store._init_db_once(str(db_store.get_db_path().resolve()))
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert not {"idx_memory_key", "idx_memory_source_claim", "idx_memory_type", "idx_memory_status"} & names
