"""Legacy-schema residue must drop exactly once, and only be recorded when it worked.

Objects left behind by removed releases are invisible from inside the tree -- no
module creates, reads or writes them -- so the tree is the only place they can be
defended.  These tests pin the properties the prune needs: it drops the recorded
set (and nothing else), it is idempotent, a complete schema still converges on the
next ``init_db()``, and a failure is never recorded as done.

The residue is recreated here from the DDL captured before the first live run, so
if a later batch removes an entry from ``_LEGACY_SCHEMA_PRUNES`` without dropping
the object, ``test_orphaned_legacy_schema_is_dropped_and_recorded`` fails instead
of the object silently returning.
"""
import sqlite3

import pytest

from vector_lake import db_store

# Verbatim pre-prune DDL, captured from the live database.
_ORPHANED_OBJECTS = (
    "CREATE TABLE ingest_jobs (filepath TEXT PRIMARY KEY, file_hash TEXT NOT NULL, "
    "job_id TEXT, job_token_hash TEXT, status TEXT NOT NULL, "
    "attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE UNIQUE INDEX idx_ingest_jobs_job_id ON ingest_jobs(job_id)",
    "CREATE INDEX idx_ingest_jobs_status ON ingest_jobs(status, updated_at)",
    "CREATE INDEX idx_jobs_retention_v6 ON jobs(status, completed_at, job_id)",
    "CREATE INDEX idx_mutation_outbox_retention_v6 "
    "ON mutation_outbox(status, completed_at, id)",
    "CREATE INDEX idx_mutation_outbox_idempotency_lookup "
    "ON mutation_outbox(idempotency_key, id DESC) WHERE idempotency_key IS NOT NULL",
    "CREATE TABLE claim_graph_nodes (node_id TEXT PRIMARY KEY, data_json TEXT, updated_at TEXT)",
)
_ORPHANED_NAMES = {
    "ingest_jobs",
    "claim_graph_nodes",
    "idx_ingest_jobs_job_id",
    "idx_ingest_jobs_status",
    "idx_jobs_retention_v6",
    "idx_mutation_outbox_retention_v6",
    "idx_mutation_outbox_idempotency_lookup",
}
_CHANGE_ID_PRUNE = "2026-09-17-drop-change-sets-change-id"


def _object_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _recorded(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM schema_migrations")}


def _forget_prunes(conn: sqlite3.Connection) -> None:
    """Return the database to its pre-upgrade state: residue present, no ledger."""
    for statement in _ORPHANED_OBJECTS:
        conn.execute(statement)
    conn.execute("DROP TABLE IF EXISTS schema_migrations")
    conn.commit()


def test_orphaned_legacy_schema_is_dropped_and_recorded(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    _forget_prunes(conn)
    assert _ORPHANED_NAMES <= _object_names(conn)

    applied = db_store.apply_legacy_schema_prunes()

    assert set(applied) == set(db_store.legacy_schema_prune_names())
    assert _ORPHANED_NAMES.isdisjoint(_object_names(conn))
    assert set(db_store.applied_schema_prunes()) == set(db_store.legacy_schema_prune_names())


def test_the_ledger_is_committed_not_left_in_an_open_transaction(isolated_memory):
    """A bare second connection is what an out-of-process check would use."""
    db_store.init_db()
    db_path = db_store.get_db_path()
    observer = sqlite3.connect(str(db_path))
    try:
        recorded = {str(row[0]) for row in observer.execute("SELECT name FROM schema_migrations")}
    finally:
        observer.close()
    assert recorded == set(db_store.legacy_schema_prune_names())


def test_prune_is_idempotent_on_a_converged_database(isolated_memory):
    db_store.init_db()
    assert db_store.apply_legacy_schema_prunes() == []
    assert db_store.apply_legacy_schema_prunes() == []


def test_the_fast_path_still_runs_pending_prunes(isolated_memory):
    """A complete schema must not mean "never prune".

    ``_schema_is_complete`` only looks at the sentinel objects, and none of the
    orphaned ones is a sentinel -- so a pre-prune database takes the fast path.
    That is exactly why the prunes cannot live behind that check.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    db_path = db_store.get_db_path()
    _forget_prunes(conn)
    # The overlay table too, so the production entry point -- not just a direct call to
    # ``apply_legacy_schema_prunes`` -- is exercised on the live-shaped state: complete schema,
    # earlier ledger rows absent, both residues present.
    for statement in _GRAM_OVERLAY_DDL:
        conn.execute(statement)
    conn.commit()
    assert _ORPHANED_NAMES <= _object_names(conn)
    assert "operational_memory_gram_overlay" in _object_names(conn)
    assert db_store._schema_is_complete(db_path) is True

    db_store._INITIALIZED_DB_PATHS.discard(str(db_path.resolve()))
    db_store.init_db()

    assert _ORPHANED_NAMES.isdisjoint(_object_names(conn))
    assert "operational_memory_gram_overlay" not in _object_names(conn), (
        "init_db is the production entry point and did not drop it"
    )
    assert set(db_store.applied_schema_prunes(conn)) == set(
        db_store.legacy_schema_prune_names()
    )


def test_a_deferred_prune_does_not_make_the_database_unusable(isolated_memory, monkeypatch):
    """A read-only or busy writer must degrade to a warning, not break every command."""
    db_store.init_db()
    conn = db_store.get_connection()
    _forget_prunes(conn)

    def _cannot_write(_conn):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(
        db_store, "_LEGACY_SCHEMA_PRUNES", ((_CHANGE_ID_PRUNE, (_cannot_write,)),)
    )
    db_store._INITIALIZED_DB_PATHS.discard(str(db_store.get_db_path().resolve()))
    db_store.init_db()  # must not raise

    assert db_store.applied_schema_prunes(conn) == {}


def test_change_sets_change_id_is_dropped_without_losing_rows(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("ALTER TABLE change_sets ADD COLUMN change_id TEXT")
    conn.execute(
        "INSERT OR REPLACE INTO change_sets (change_set_id, data_json, updated_at, change_id) "
        "VALUES ('cs1', '{}', '2026-09-17T00:00:00+00:00', 'unreachable')"
    )
    conn.execute("DELETE FROM schema_migrations")
    conn.commit()

    applied = db_store.apply_legacy_schema_prunes()

    assert _CHANGE_ID_PRUNE in applied
    assert "change_id" not in _columns(conn, "change_sets")
    assert conn.execute("SELECT COUNT(*) FROM change_sets").fetchone()[0] == 1


def test_a_column_drop_on_a_database_without_it_is_tolerated(isolated_memory):
    """The normal fresh-database case: the column never existed, so it is already done."""
    db_store.init_db()
    conn = db_store.get_connection()
    assert "change_id" not in _columns(conn, "change_sets")

    conn.execute("DELETE FROM schema_migrations")
    conn.commit()

    assert _CHANGE_ID_PRUNE in db_store.apply_legacy_schema_prunes()


def test_an_indexed_column_blocks_the_drop_and_nothing_is_recorded(isolated_memory):
    """Regression from independent review.

    SQLite reports a ``DROP COLUMN`` that cannot proceed because the column is
    referenced by an index as
    ``error in index ... after drop column: no such column: ...``.  A bare
    substring test for ``no such column`` therefore tolerated a *real* failure
    and recorded the migration as applied, leaving the column in place with the
    ledger claiming otherwise.  The probe-based guard removes the ambiguity.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("ALTER TABLE change_sets ADD COLUMN change_id TEXT")
    conn.execute("CREATE INDEX idx_change_sets_change_id_probe ON change_sets(change_id)")
    conn.execute("DELETE FROM schema_migrations")
    conn.commit()

    with pytest.raises(sqlite3.OperationalError):
        db_store.apply_legacy_schema_prunes()

    assert "change_id" in _columns(conn, "change_sets")
    assert _CHANGE_ID_PRUNE not in _recorded(conn)


def test_a_failed_step_leaves_its_migration_unrecorded(isolated_memory, monkeypatch):
    """A failure must degrade to "retry next time", never to a recorded success."""
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("DELETE FROM schema_migrations")
    conn.commit()

    def _boom(target: sqlite3.Connection) -> None:
        target.execute("DROP TABLE definitely_not_a_real_table")

    monkeypatch.setattr(
        db_store, "_LEGACY_SCHEMA_PRUNES", (("deliberately-broken", (_boom,)),)
    )
    with pytest.raises(sqlite3.OperationalError):
        db_store.apply_legacy_schema_prunes()

    assert "deliberately-broken" not in _recorded(conn)


def test_a_ledger_read_failure_is_not_treated_as_nothing_applied(isolated_memory):
    """Regression: swallowing the read would re-run finished migrations.

    A swallowed error reads as "empty ledger", so the caller re-applies every
    migration and overwrites the original ``applied_at`` values -- at which point
    the ledger records when it last ran, not when the migration first ran.
    ``_ledger_exists`` probes ``sqlite_master``, so there is no read error left to
    swallow.
    """
    db_store.init_db()
    assert db_store.applied_schema_prunes(db_store.get_connection())

    closed = sqlite3.connect(str(db_store.get_db_path()))
    closed.close()
    with pytest.raises(sqlite3.Error):
        db_store._recorded_prunes(closed)


def test_one_failing_migration_does_not_undo_an_earlier_one(isolated_memory, monkeypatch):
    """Each migration owns its transaction, so progress is kept."""
    db_store.init_db()
    conn = db_store.get_connection()

    def _succeeds(target: sqlite3.Connection) -> None:
        target.execute("SELECT 1")

    def _boom(target: sqlite3.Connection) -> None:
        target.execute("DROP TABLE definitely_not_a_real_table")

    monkeypatch.setattr(
        db_store,
        "_LEGACY_SCHEMA_PRUNES",
        (("first-migration", (_succeeds,)), ("deliberately-broken", (_boom,))),
    )
    with pytest.raises(sqlite3.OperationalError):
        db_store.apply_legacy_schema_prunes()

    assert "first-migration" in _recorded(conn)
    assert "deliberately-broken" not in _recorded(conn)


# The overlay table's pre-prune DDL, verbatim from before this batch removed it.  Recreated
# here for the same reason as the residue above: if the ledger entry were dropped while a
# database could still hold the table, this fails instead of the table quietly surviving.
_GRAM_OVERLAY_DDL = (
    "CREATE TABLE operational_memory_gram_overlay ("
    "gram TEXT NOT NULL, doc INTEGER NOT NULL, mask INTEGER NOT NULL, "
    "PRIMARY KEY (gram, doc)) WITHOUT ROWID",
    "CREATE INDEX idx_om_gram_overlay_doc ON operational_memory_gram_overlay (doc)",
)


def test_the_gram_overlay_is_dropped_by_its_prune(isolated_memory):
    """The table is gone from the current schema, so only the prune can remove it."""
    db_store.init_db()
    conn = db_store.get_connection()

    assert "operational_memory_gram_overlay" not in _object_names(conn), (
        "the current schema still creates it, so the prune is not what removes it"
    )
    for statement in _GRAM_OVERLAY_DDL:
        conn.execute(statement)
    conn.commit()
    conn.execute("DELETE FROM schema_migrations WHERE name = '2026-09-18-drop-gram-overlay'")
    conn.commit()
    assert "operational_memory_gram_overlay" in _object_names(conn)

    applied = db_store.apply_legacy_schema_prunes()

    assert "2026-09-18-drop-gram-overlay" in applied
    assert "operational_memory_gram_overlay" not in _object_names(conn)
    assert "idx_om_gram_overlay_doc" not in _object_names(conn)
    assert "2026-09-18-drop-gram-overlay" in _recorded(conn)
    # Idempotent: a second call applies nothing and the ledger is unchanged.
    assert db_store.apply_legacy_schema_prunes() == []


# The removed table's pre-prune DDL, verbatim from before the removal.  Recreated here for the same
# reason as the residue above: if the ledger entry were dropped while a database could still hold the
# table, this fails instead of the table quietly surviving.
_PAGE_GRAPH_EDGES_PRUNE = "2026-09-18-drop-page-graph-edges"
_PAGE_GRAPH_EDGES_DDL = (
    "CREATE TABLE page_graph_edges (source_id TEXT, target_id TEXT, relation TEXT, "
    "weight REAL, updated_at TEXT, PRIMARY KEY (source_id, target_id, relation))",
)


def test_the_second_edge_table_is_dropped_by_its_prune(isolated_memory):
    """The current schema never creates it, so only the prune can remove it."""
    db_store.init_db()
    conn = db_store.get_connection()

    assert "page_graph_edges" not in _object_names(conn), (
        "the current schema still creates it, so the prune is not what removes it"
    )
    for statement in _PAGE_GRAPH_EDGES_DDL:
        conn.execute(statement)
    conn.execute("DELETE FROM schema_migrations WHERE name = ?", (_PAGE_GRAPH_EDGES_PRUNE,))
    conn.commit()
    assert "page_graph_edges" in _object_names(conn)

    applied = db_store.apply_legacy_schema_prunes()

    assert _PAGE_GRAPH_EDGES_PRUNE in applied
    assert "page_graph_edges" not in _object_names(conn)
    assert _PAGE_GRAPH_EDGES_PRUNE in _recorded(conn)
    assert db_store.apply_legacy_schema_prunes() == []
