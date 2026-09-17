import sqlite3
import threading
import time

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


def test_transaction_gives_up_inside_the_lock_budget(isolated_memory, monkeypatch):
    """A held write lock must surface as a bounded timeout, not a hang.

    The previous retry loop was 60 attempts against the 30 s connect-time busy
    timeout, i.e. one ``transaction()`` could park for roughly half an hour.
    """
    db_store.init_db()
    monkeypatch.setattr(db_store, "BEGIN_LOCK_BUDGET_SECONDS", 2.0)
    monkeypatch.setattr(db_store, "DB_BUSY_TIMEOUT_MS", 200)

    blocker = sqlite3.connect(str(db_store.get_db_path()), timeout=0.2, isolation_level=None)
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(db_store.DatabaseLockTimeout) as caught:
            with db_store.transaction():
                pytest.fail("the write lock was never free")
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()

    assert 1.0 <= elapsed < 10.0, f"lock wait was not bounded: {elapsed:.2f}s"
    assert "write lock" in str(caught.value)
    # The timeout is a contention signal, not a broken database.
    assert isinstance(caught.value, db_store.DatabaseLockTimeout)


def test_lock_wait_is_retried_once_the_writer_releases(isolated_memory, monkeypatch):
    """Contention inside the budget must succeed rather than raise."""
    db_store.init_db()
    conn = db_store.get_connection()

    blocker = sqlite3.connect(
        str(db_store.get_db_path()), timeout=0.2, isolation_level=None, check_same_thread=False
    )
    blocker.execute("BEGIN IMMEDIATE")
    # A stuck release must not cost the full production budget.
    monkeypatch.setattr(db_store, "BEGIN_LOCK_BUDGET_SECONDS", 10.0)

    def release_soon():
        time.sleep(0.3)
        blocker.rollback()
        blocker.close()

    threading.Thread(target=release_soon, daemon=True).start()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO entities (entity_id, canonical_name, data_json) "
            "VALUES ('after_wait', 'After Wait', '{}')"
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_id = 'after_wait'"
    ).fetchone()[0] == 1
    # The full default patience is restored after the retry loop hands the lock back.
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db_store.DB_BUSY_TIMEOUT_MS


class _CommitFailingConnection:
    """Delegating proxy whose ``commit()`` fails, to reach the rollback branch."""

    def __init__(self, inner: sqlite3.Connection):
        self._inner = inner
        self.fail = True

    def commit(self):
        if self.fail:
            raise sqlite3.OperationalError("inject commit failure")
        return self._inner.commit()

    @property
    def inner(self) -> sqlite3.Connection:
        return self._inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_failed_commit_rolls_back_so_the_connection_stays_usable(isolated_memory, monkeypatch):
    """A failed ``commit()`` must not leave an open transaction behind."""
    db_store.init_db()
    proxy = _CommitFailingConnection(db_store.get_connection())
    monkeypatch.setattr(db_store, "get_connection", lambda: proxy)

    with pytest.raises(sqlite3.OperationalError, match="inject commit failure"):
        with db_store.transaction():
            proxy.execute(
                "INSERT INTO entities (entity_id, canonical_name, data_json) "
                "VALUES ('leaked', 'Leaked', '{}')"
            )

    inner = proxy.inner
    assert inner.in_transaction is False, "commit failure left the connection in a transaction"
    assert inner.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_id = 'leaked'"
    ).fetchone()[0] == 0

    proxy.fail = False
    with db_store.transaction():
        inner.execute(
            "INSERT INTO entities (entity_id, canonical_name, data_json) "
            "VALUES ('ok', 'Ok', '{}')"
        )
    assert inner.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_id = 'ok'"
    ).fetchone()[0] == 1
