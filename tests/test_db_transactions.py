import sqlite3
import threading

import pytest

from vector_lake import db_store, governance_store


def test_nested_store_calls_roll_back_with_outer_transaction(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()

    with pytest.raises(RuntimeError, match="inject rollback"):
        with db_store.transaction():
            governance_store.upsert_entity(
                "entity_rollback",
                {
                    "entity_id": "entity_rollback",
                    "page_key": "Concept_Rollback",
                    "canonical_name": "Rollback",
                    "type": "concept",
                    "status": "Active",
                },
            )
            governance_store.save_alias_registry({"items": {"Rollback": "entity_rollback"}})
            raise RuntimeError("inject rollback")

    assert conn.execute("SELECT 1 FROM entities WHERE entity_id = 'entity_rollback'").fetchone() is None
    assert conn.execute("SELECT 1 FROM alias_registry WHERE value = 'entity_rollback'").fetchone() is None


def test_init_db_runs_schema_work_once_per_database_path(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr(
        db_store,
        "_init_db_once",
        lambda _key: (_ for _ in ()).throw(AssertionError("schema rerun")),
    )
    db_store.init_db()


def test_targeted_alias_accessors_participate_in_transactions(isolated_memory):
    db_store.init_db()

    governance_store.upsert_alias("entity_old", "entity_canonical")
    assert governance_store.get_alias("entity_old") == "entity_canonical"

    with pytest.raises(RuntimeError, match="inject rollback"):
        with db_store.transaction():
            governance_store.upsert_alias("entity_rollback", "entity_canonical")
            raise RuntimeError("inject rollback")

    assert governance_store.get_alias("entity_rollback") is None


def test_connection_reopens_when_database_path_changes(tmp_path, monkeypatch):
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(first))
    first_connection = db_store.get_connection()
    first_connection.execute("CREATE TABLE marker (value TEXT)")
    first_connection.commit()

    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(second))
    second_connection = db_store.get_connection()

    assert second_connection is not first_connection
    assert second_connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'marker'"
    ).fetchone() is None


def test_close_all_connections_releases_worker_handles(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(tmp_path / "workers.db"))
    opened = []

    def open_worker_connection():
        connection = db_store.get_connection()
        opened.append(connection)
        connection.execute("SELECT 1").fetchone()

    thread = threading.Thread(target=open_worker_connection)
    thread.start()
    thread.join()

    db_store.close_all_connections()

    assert opened
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")
