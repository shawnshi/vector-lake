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
