"""The per-connection SQLite pager settings that carry the cold-read cost."""

from __future__ import annotations

import pytest

from vector_lake import db_store


class _StubCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _StubConnection:
    """Minimal connection double that answers the two PRAGMA reads."""

    def __init__(self, *, cache_row=(-65_536,), mmap_row=(0,)):
        self._cache_row = cache_row
        self._mmap_row = mmap_row
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str):
        self.executed.append(sql)
        if sql.startswith("PRAGMA cache_size"):
            return _StubCursor(self._cache_row)
        if sql.startswith("PRAGMA mmap_size"):
            return _StubCursor(self._mmap_row)
        return _StubCursor(None)

    def close(self):
        self.closed = True


def test_cache_and_mmap_defaults_pin_the_settings_that_were_left_unset():
    assert db_store.configured_sqlite_cache_kib() == 65_536
    # Off by default: SQLite forbids a memory-mapped file being truncated by
    # another process, and several host adapters share this database.
    assert db_store.configured_sqlite_mmap_bytes() == 0


def test_cache_size_env_override_is_bounded_below_and_above(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_SQLITE_CACHE_KIB", "131072")
    assert db_store.configured_sqlite_cache_kib() == 131_072

    # A request below SQLite's 2 MiB default would make the multi-GiB corpus
    # slower than shipping no explicit setting at all.
    monkeypatch.setenv("VECTOR_LAKE_SQLITE_CACHE_KIB", "16")
    assert db_store.configured_sqlite_cache_kib() == db_store._SQLITE_CACHE_KIB_FLOOR

    monkeypatch.setenv("VECTOR_LAKE_SQLITE_CACHE_KIB", "99999999999")
    assert (
        db_store.configured_sqlite_cache_kib()
        == db_store._SQLITE_CACHE_KIB_CEILING
    )


def test_cache_size_env_rejects_non_numeric_input(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_SQLITE_CACHE_KIB", "not-a-number")
    assert db_store.configured_sqlite_cache_kib() == 65_536


def test_get_connection_applies_cache_size_to_read_write_and_read_only(
    isolated_memory, monkeypatch
):
    db_store.init_db()
    write_connection = db_store.get_connection()
    assert write_connection.execute("PRAGMA cache_size").fetchone()[0] == -65_536

    db_store.close_all_connections()
    monkeypatch.setenv("VECTOR_LAKE_MCP_SURFACE", "readonly")
    read_connection = db_store.get_connection()
    assert read_connection.execute("PRAGMA cache_size").fetchone()[0] == -65_536
    assert read_connection.execute("PRAGMA query_only").fetchone()[0] == 1


def test_mmap_is_applied_only_when_requested(isolated_memory, monkeypatch):
    db_store.init_db()
    default_connection = db_store.get_connection()
    assert default_connection.execute("PRAGMA mmap_size").fetchone()[0] == 0

    db_store.close_all_connections()
    monkeypatch.setenv("VECTOR_LAKE_SQLITE_MMAP_BYTES", "1073741824")
    requested = db_store.get_connection()
    assert requested.execute("PRAGMA mmap_size").fetchone()[0] > 0


def test_pager_configuration_fails_loud_when_cache_size_is_not_applied(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_SQLITE_CACHE_KIB", "65536")
    connection = _StubConnection(cache_row=(-2000,))

    with pytest.raises(RuntimeError) as error:
        db_store._configure_sqlite_pager(connection)

    assert "page cache configuration was not applied" in str(error.value)
    assert connection.closed is True


def test_pager_configuration_fails_loud_when_requested_mmap_is_not_applied(
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_SQLITE_CACHE_KIB", "65536")
    monkeypatch.setenv("VECTOR_LAKE_SQLITE_MMAP_BYTES", "1073741824")
    connection = _StubConnection(cache_row=(-65_536,), mmap_row=(0,))

    with pytest.raises(RuntimeError) as error:
        db_store._configure_sqlite_pager(connection)

    assert "mmap configuration was not applied" in str(error.value)
    assert connection.closed is True


def test_pager_configuration_leaves_mmap_alone_at_the_default(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_SQLITE_CACHE_KIB", "65536")
    connection = _StubConnection(mmap_row=(-1,))

    db_store._configure_sqlite_pager(connection)

    assert connection.closed is False
    assert not any(sql.startswith("PRAGMA mmap_size") for sql in connection.executed)
