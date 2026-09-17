"""Legacy-schema residue must drop exactly once, and only be recorded when it worked.

Objects left behind by removed releases are invisible from inside the tree -- no
module creates, reads or writes them -- so the tree is the only place they can be
defended.  These tests pin the four properties the prune needs: it drops the
recorded set (and nothing else), it is idempotent, a database whose sentinels are
all present but whose ledger is missing does **not** take the ``init_db()`` fast
path, and a failed statement is never recorded as done.

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


def _object_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}


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
    """A bare second connection is what ``_schema_is_complete`` uses; it must see it."""
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


def test_a_pending_prune_defeats_the_init_fast_path(isolated_memory):
    """Without this the fast path would skip the prunes forever on a live database."""
    db_store.init_db()
    db_path = db_store.get_db_path()
    conn = db_store.get_connection()
    assert db_store._schema_is_complete(db_path) is True

    conn.execute("DELETE FROM schema_migrations")
    conn.commit()
    assert db_store._schema_is_complete(db_path) is False


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

    assert "2026-09-17-drop-change-sets-change-id" in applied
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(change_sets)")}
    assert "change_id" not in columns
    assert conn.execute("SELECT COUNT(*) FROM change_sets").fetchone()[0] == 1


def test_a_column_drop_on_a_database_without_it_is_tolerated(isolated_memory):
    """The normal fresh-database case: the column never existed, so it is already done."""
    db_store.init_db()
    conn = db_store.get_connection()
    assert "change_id" not in {
        str(row[1]) for row in conn.execute("PRAGMA table_info(change_sets)")
    }

    conn.execute("DELETE FROM schema_migrations")
    conn.commit()

    assert "2026-09-17-drop-change-sets-change-id" in db_store.apply_legacy_schema_prunes()


def test_a_failed_statement_leaves_its_migration_unrecorded(isolated_memory, monkeypatch):
    """A failure must degrade to "retry next init", never to a recorded success.

    ``DROP TABLE`` without ``IF EXISTS`` over an absent table is a real failure,
    not a tolerated one -- tolerating it would let a broken statement mark itself
    done.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("DELETE FROM schema_migrations")
    conn.commit()

    monkeypatch.setattr(
        db_store,
        "_LEGACY_SCHEMA_PRUNES",
        (("deliberately-broken", ("DROP TABLE definitely_not_a_real_table",)),),
    )
    with pytest.raises(sqlite3.OperationalError):
        db_store.apply_legacy_schema_prunes()

    recorded = {str(row[0]) for row in conn.execute("SELECT name FROM schema_migrations")}
    assert "deliberately-broken" not in recorded
