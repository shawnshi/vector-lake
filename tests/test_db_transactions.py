import subprocess
import sqlite3
import sys
import time
import threading
from pathlib import Path

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

def test_runtime_generation_tracks_same_size_entity_mutations(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()

    def generation():
        return int(
            conn.execute(
                "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
            ).fetchone()[0]
        )

    initial = generation()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO entities (entity_id, canonical_name, data_json, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "entity_generation",
                "Generation",
                '{"entity_id":"entity_generation","page_key":"Concept_AA"}',
                "2026-07-27T00:00:00+00:00",
            ),
        )
    after_insert = generation()

    with db_store.transaction():
        conn.execute(
            "UPDATE entities SET data_json = ? WHERE entity_id = ?",
            (
                '{"entity_id":"entity_generation","page_key":"Concept_BB"}',
                "entity_generation",
            ),
        )
    after_same_size_update = generation()

    with db_store.transaction():
        conn.execute("DELETE FROM entities WHERE entity_id = ?", ("entity_generation",))
    after_delete = generation()

    assert after_insert == initial + 1
    assert after_same_size_update == after_insert + 1
    assert after_delete == after_same_size_update + 1

    rollback_generation = generation()
    with pytest.raises(RuntimeError, match="rollback generation"):
        with db_store.transaction():
            conn.execute(
                "INSERT INTO entities (entity_id, data_json) VALUES (?, ?)",
                ("entity_rolled_back", '{"page_key":"Concept_Rollback"}'),
            )
            raise RuntimeError("rollback generation")

    assert generation() == rollback_generation


def test_bulk_write_bumps_runtime_generation_once(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    before = int(
        conn.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    )

    with db_store.transaction():
        conn.executemany(
            "INSERT INTO entities (entity_id, data_json) VALUES (?, ?)",
            (
                (f"entity_bulk_{index}", '{"page_key":"Concept_Bulk"}')
                for index in range(250)
            ),
        )

    after = int(
        conn.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    )
    assert after == before + 1


def test_partial_executemany_commit_still_bumps_runtime_generation(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    before = int(
        conn.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.executemany(
            "INSERT INTO entities (entity_id, data_json) VALUES (?, ?)",
            [
                ("entity_partial", '{"page_key":"Concept_Partial"}'),
                ("entity_partial", '{"page_key":"Concept_Duplicate"}'),
            ],
        )
    conn.commit()

    after = int(
        conn.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_id = 'entity_partial'"
    ).fetchone()[0] == 1
    assert after == before + 1


def test_runtime_generation_tracks_cte_comments_and_qualified_tables(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    before = int(
        conn.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    )

    with db_store.transaction():
        conn.cursor().execute(
            "-- leading comment\n"
            "INSERT INTO main.entities (entity_id, data_json) VALUES (?, ?)",
            ("entity_commented", '{"page_key":"Concept_Commented"}'),
        )
        conn.execute(
            "WITH payload(entity_id, data_json) AS (VALUES (?, ?)) "
            "INSERT INTO main.entities (entity_id, data_json) "
            "SELECT entity_id, data_json FROM payload",
            ("entity_cte", '{"page_key":"Concept_CTE"}'),
        )

    after = int(
        conn.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    )
    assert after == before + 1
    assert conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_id IN "
        "('entity_commented', 'entity_cte')"
    ).fetchone()[0] == 2


def test_runtime_generation_tracks_connection_and_cursor_executescript(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()

    def generation():
        return int(
            conn.execute(
                "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
            ).fetchone()[0]
        )

    before = generation()
    conn.executescript(
        "INSERT INTO main.entities (entity_id, data_json) "
        "VALUES ('entity_script_connection', '{\"page_key\":\"Concept_Script_A\"}');"
    )
    after_connection = generation()
    conn.cursor().executescript(
        "INSERT INTO main.entities (entity_id, data_json) "
        "VALUES ('entity_script_cursor', '{\"page_key\":\"Concept_Script_B\"}');"
    )
    after_cursor = generation()

    assert after_connection == before + 1
    assert after_cursor == after_connection + 1


def test_nested_rollback_does_not_bump_runtime_generation(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    before = int(
        conn.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    )

    with db_store.transaction():
        with pytest.raises(RuntimeError, match="nested generation rollback"):
            with db_store.transaction():
                conn.execute(
                    "INSERT INTO entities (entity_id, data_json) VALUES (?, ?)",
                    ("nested_generation", '{"page_key":"Concept_Nested"}'),
                )
                raise RuntimeError("nested generation rollback")
        conn.execute(
            "INSERT INTO alias_registry (key, value, updated_at) VALUES (?, ?, ?)",
            ("nested_alias", "value", "2026-07-27T00:00:00+00:00"),
        )

    after = int(
        conn.execute(
            "SELECT generation FROM runtime_generations WHERE surface = 'entities'"
        ).fetchone()[0]
    )
    assert after == before
    assert (
        conn.execute("SELECT 1 FROM entities WHERE entity_id = 'nested_generation'")
        .fetchone()
        is None
    )


def test_runtime_generation_installs_all_write_health_surfaces(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    expected = {
        "claim_graph_edges",
        "claims",
        "entities",
        "page_graph_edges",
        "sources",
        "timeline_events",
        "mutation_outbox",
        "jobs",
    }
    generations = {
        str(row[0])
        for row in conn.execute("SELECT surface FROM runtime_generations")
    }
    assert expected <= generations
    assert isinstance(conn, db_store._GenerationTrackingConnection)
    trigger_names = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    assert not any(
        name.startswith("trg_") and "_generation_v1_" in name
        for name in trigger_names
    )


def test_targeted_alias_accessors_participate_in_transactions(isolated_memory):
    db_store.init_db()

    governance_store.upsert_alias("entity_old", "entity_canonical")
    assert governance_store.get_alias("entity_old") == "entity_canonical"

    with pytest.raises(RuntimeError, match="inject rollback"):
        with db_store.transaction():
            governance_store.upsert_alias("entity_rollback", "entity_canonical")
            raise RuntimeError("inject rollback")

    assert governance_store.get_alias("entity_rollback") is None


def test_base_exception_rolls_back_and_releases_transaction_state(isolated_memory):
    db_store.init_db()

    with pytest.raises(KeyboardInterrupt, match="interrupt transaction"):
        with db_store.transaction():
            governance_store.upsert_alias("entity_interrupted", "entity_target")
            raise KeyboardInterrupt("interrupt transaction")

    assert governance_store.get_alias("entity_interrupted") is None
    with db_store.transaction():
        governance_store.upsert_alias("entity_after_interrupt", "entity_target")
    assert governance_store.get_alias("entity_after_interrupt") == "entity_target"


def test_connection_reopens_when_database_path_changes(tmp_path, monkeypatch):
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(first))
    first_connection = db_store.get_connection()
    assert first_connection.execute("SELECT vec_version()").fetchone()[0]
    first_connection.execute("CREATE TABLE marker (value TEXT)")
    first_connection.commit()

    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(second))
    second_connection = db_store.get_connection()

    assert second_connection is not first_connection
    assert second_connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'marker'"
    ).fetchone() is None
    assert second_connection.execute("SELECT vec_version()").fetchone()[0]


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


def test_worker_connection_closes_automatically_when_thread_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(tmp_path / "thread-exit.db"))
    opened = []

    def use_worker_connection():
        first = db_store.get_connection()
        second = db_store.get_connection()
        assert second is first
        opened.append(first)
        first.execute("SELECT 1").fetchone()

    thread = threading.Thread(target=use_worker_connection)
    thread.start()
    thread.join()

    assert opened
    assert id(opened[0]) not in db_store._CONNECTIONS
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_connection_scope_reuses_then_closes_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(tmp_path / "call-scope.db"))

    with db_store.connection_scope() as connection:
        assert db_store.get_connection() is connection
        with db_store.connection_scope() as nested:
            assert nested is connection
        with db_store.transaction() as transaction_connection:
            assert transaction_connection is connection
            transaction_connection.execute("CREATE TABLE scoped_marker (value TEXT)")
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'scoped_marker'"
        ).fetchone()

    assert id(connection) not in db_store._CONNECTIONS
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_connection_scope_inside_transaction_defers_close(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(tmp_path / "transaction-scope.db"))

    with db_store.transaction() as connection:
        with db_store.connection_scope() as scoped:
            assert scoped is connection
        connection.execute("CREATE TABLE deferred_close (value TEXT)")

    assert id(connection) not in db_store._CONNECTIONS
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_importing_db_store_does_not_eagerly_import_sqlite_vec():
    repository_root = Path(__file__).resolve().parents[1]
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import vector_lake.db_store; "
                "print(','.join(name for name in ('sqlite_vec', 'numpy') "
                "if name in sys.modules))"
            ),
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=True,
    )

    assert probe.stdout.strip() == ""


def test_transaction_lock_wait_honors_max_wait(tmp_path, monkeypatch):
    db_path = tmp_path / "deadline.db"
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(db_path))
    db_store.init_db()
    db_store.close_connection()

    locker = sqlite3.connect(str(db_path), timeout=0.1)
    locker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="deadline exceeded"):
            with db_store.transaction(max_wait_seconds=0.05):
                pass
    finally:
        elapsed = time.monotonic() - started
        locker.rollback()
        locker.close()

    assert elapsed < 0.5
    with db_store.transaction(max_wait_seconds=0.5) as connection:
        connection.execute("CREATE TABLE deadline_recovered (value TEXT)")
    assert db_store.get_connection().execute(
        "SELECT name FROM sqlite_master WHERE name = 'deadline_recovered'"
    ).fetchone()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("-1", 0.05),
        ("9999", 300.0),
        ("invalid", 30.0),
        ("nan", 30.0),
    ],
)
def test_transaction_default_wait_configuration_is_finite_and_bounded(
    monkeypatch,
    configured,
    expected,
):
    monkeypatch.setenv(
        "VECTOR_LAKE_SQLITE_WRITE_MAX_WAIT_SECONDS",
        configured,
    )

    assert db_store._configured_transaction_max_wait_seconds() == expected


def test_transaction_lock_wait_uses_configured_default(tmp_path, monkeypatch):
    db_path = tmp_path / "configured-deadline.db"
    monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(db_path))
    monkeypatch.setenv("VECTOR_LAKE_SQLITE_WRITE_MAX_WAIT_SECONDS", "0.05")
    db_store.init_db()
    db_store.close_connection()

    locker = sqlite3.connect(str(db_path), timeout=0.1)
    locker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="deadline exceeded"):
            with db_store.transaction():
                pass
    finally:
        elapsed = time.monotonic() - started
        locker.rollback()
        locker.close()

    assert elapsed < 0.5

def test_caught_nested_transaction_failure_rolls_back_to_savepoint(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("CREATE TABLE nested_values (value INTEGER)")
    conn.commit()

    with db_store.transaction():
        conn.execute("INSERT INTO nested_values VALUES (1)")
        try:
            with db_store.transaction():
                conn.execute("INSERT INTO nested_values VALUES (2)")
                raise RuntimeError("rollback nested")
        except RuntimeError:
            pass
        conn.execute("INSERT INTO nested_values VALUES (3)")

    values = [row[0] for row in conn.execute("SELECT value FROM nested_values ORDER BY value")]
    assert values == [1, 3]
    assert getattr(db_store._LOCAL, "in_transaction", False) is False
    assert getattr(db_store._LOCAL, "transaction_depth", 0) == 0


def test_successful_deadline_transaction_restores_busy_timeout(
    isolated_memory,
):
    db_store.init_db()
    conn = db_store.get_connection()
    original_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    with db_store.transaction(max_wait_seconds=0.5):
        conn.execute("CREATE TABLE restored_timeout (value TEXT)")

    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == original_timeout
    assert getattr(db_store._LOCAL, "in_transaction", False) is False
    assert getattr(db_store._LOCAL, "transaction_depth", 0) == 0
