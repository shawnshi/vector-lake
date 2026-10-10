"""Contention telemetry must not extend a successfully acquired SQLite lock."""
import inspect
import json
import sqlite3
import threading
import time

import pytest

from vector_lake import db_store
from vector_lake.wiki_utils import get_meta_dir


def test_successful_contention_is_reported_after_commit(isolated_memory, monkeypatch):
    db_store.init_db()
    conn = db_store.get_connection()
    monkeypatch.setattr(db_store, "DB_BUSY_TIMEOUT_MS", 20)
    monkeypatch.setattr(db_store, "BEGIN_LOCK_BUDGET_SECONDS", 2.0)
    blocker = sqlite3.connect(str(db_store.get_db_path()), isolation_level=None, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    report = []

    def record(*args):
        report.append((conn.in_transaction, conn.execute("SELECT COUNT(*) FROM entities WHERE entity_id = 'reported'").fetchone()[0], args))

    monkeypatch.setattr(db_store, "_record_lock_contention", record)

    def release():
        time.sleep(0.1)
        blocker.rollback()
        blocker.close()

    releaser = threading.Thread(target=release)
    releaser.start()
    try:
        with db_store.transaction():
            conn.execute("INSERT INTO entities (entity_id, canonical_name, data_json) VALUES ('reported', 'Reported', '{}')")
    finally:
        releaser.join(3)
    assert report and report[0][2][-1] == "acquired-after-retry"
    assert report[0][:2] == (False, 1), "diagnostic ran before the write lock was released"


def test_contention_report_does_not_expand_source_code_stack(isolated_memory, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("inspect.stack opens source files")

    monkeypatch.setattr(inspect, "stack", forbidden)
    db_store._record_lock_contention(2, 2.0, 0.1, "acquired-after-retry")
    payload = json.loads((get_meta_dir() / "runtime" / "write_lock_contention.json").read_text(encoding="utf-8"))
    assert payload["last"]["caller"] == "test_contention_report_does_not_expand_source_code_stack"


def test_begin_budget_clips_a_larger_busy_timeout(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setattr(db_store, "DB_BUSY_TIMEOUT_MS", 5000)
    monkeypatch.setattr(db_store, "BEGIN_LOCK_BUDGET_SECONDS", 0.2)
    blocker = sqlite3.connect(str(db_store.get_db_path()), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    started = time.perf_counter()
    try:
        with pytest.raises(db_store.DatabaseLockTimeout):
            with db_store.transaction():
                pytest.fail("the blocker still owns the write lock")
        assert 0.15 <= time.perf_counter() - started < 0.8
        assert not db_store.get_connection().in_transaction
        assert db_store.get_connection().execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        blocker.rollback()
        blocker.close()


def test_busy_timeout_restored_on_non_lock_error(isolated_memory, monkeypatch):
    db_store.init_db()
    conn = db_store.get_connection()
    monkeypatch.setattr(db_store, "DB_BUSY_TIMEOUT_MS", 5000)
    monkeypatch.setattr(db_store, "BEGIN_LOCK_BUDGET_SECONDS", 0.2)

    class FailingBegin:
        def execute(self, sql):
            if sql == "BEGIN IMMEDIATE":
                raise sqlite3.OperationalError("not a lock error")
            return conn.execute(sql)

    with pytest.raises(sqlite3.OperationalError, match="not a lock error"):
        db_store._acquire_write_lock(FailingBegin())
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_read_snapshot_rejects_writes_and_restores_connection_after_failure(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with db_store.read_snapshot():
            conn.execute("SELECT COUNT(*) FROM entities").fetchone()
            conn.execute("INSERT INTO entities (entity_id, canonical_name, data_json) VALUES ('forbidden', 'Forbidden', '{}')")
    assert not conn.in_transaction
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
    with db_store.transaction():
        conn.execute("INSERT INTO entities (entity_id, canonical_name, data_json) VALUES ('allowed', 'Allowed', '{}')")
    with db_store.transaction():
        with pytest.raises(RuntimeError, match="outside a SQLite transaction"):
            with db_store.read_snapshot():
                pytest.fail("nested snapshot committed its caller")
        assert conn.in_transaction


def test_read_snapshot_preserves_existing_query_only_mode(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("PRAGMA query_only = ON")
    try:
        with pytest.raises(RuntimeError, match="read body failed"):
            with db_store.read_snapshot():
                conn.execute("SELECT COUNT(*) FROM entities").fetchone()
                raise RuntimeError("read body failed")
        assert not conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.execute("PRAGMA query_only = OFF")

