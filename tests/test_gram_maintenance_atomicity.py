"""A maintenance operation reads its snapshot inside the transaction it writes with.

``rebuild_memory_gram_index`` used to stage in committed batches and clear the queue in a
*later* transaction.  Between the two, a writer on another connection could commit a
change whose marker the clear then erased: the base would hold the pre-change grams of a
document with nothing left to make the read path skip them, and after that rebuild the
index reported itself usable.  ``prune_retired_gram_docs`` read its set of retired
documents outside its transaction for the same reason.

The fix is one transaction per operation, which means the writer is refused rather than
interleaved.  These tests drive the real functions and, from a second connection, check
that the write lock is actually held while the snapshot is being taken -- the refusal is
the observable that says the window is closed.
"""

import sqlite3
import threading

import pytest

from tests.test_gram_index_exactness import _put

from vector_lake import db_store, memory_gram_index


def _seed(count: int = 4) -> None:
    db_store.init_db()
    conn = db_store.get_connection()
    for index in range(count):
        _put(conn, f"mem_{index}", f"医院 电子病历 六级 目标 {index}")


def _blocked_writer() -> None:
    """Assert that a second connection cannot take the write lock right now."""
    other = sqlite3.connect(db_store.get_db_path())
    try:
        other.execute("PRAGMA busy_timeout = 250")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
    finally:
        other.close()


def _run_holding_lock_at(monkeypatch, hook_name: str, target) -> None:
    """Run ``target`` on a worker thread, stopping inside its first call to ``hook_name``.

    The stop is what makes the assertion below deterministic: at that point the
    operation is mid-transaction with its snapshot already taken.
    """
    inside = threading.Event()
    release = threading.Event()
    original = getattr(memory_gram_index, hook_name)
    calls = {"count": 0}

    def hook(*args, **kwargs):
        if calls["count"] == 0:
            calls["count"] += 1
            inside.set()
            assert release.wait(30), "the test never released the operation"
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_gram_index, hook_name, hook)
    outcome: dict[str, object] = {}

    def run():
        try:
            outcome["result"] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="maintenance")
    worker.start()
    try:
        assert inside.wait(30), "the operation never reached its snapshot"
        _blocked_writer()
    finally:
        release.set()
        worker.join(60)

    assert not worker.is_alive(), "the maintenance thread did not finish"
    assert "error" not in outcome, outcome.get("error")
    return outcome["result"]


def test_the_rebuild_excludes_a_writer_while_it_stages(isolated_memory, monkeypatch):
    _seed()

    result = _run_holding_lock_at(
        monkeypatch, "extract_grams", memory_gram_index.rebuild_memory_gram_index
    )

    assert "Rebuilt the memory gram index" in result
    assert memory_gram_index.gram_index_usable() is True


def test_the_prune_excludes_a_writer_while_it_scans(isolated_memory, monkeypatch):
    """Pins the invariant; it does not distinguish the pre-fix behaviour.

    The prune's scan already ran inside its transaction before the fix -- what was
    outside was the *snapshot* that decides which documents are retired, and there is no
    call between that read and the transaction to hang an observer on, so this test
    passes either way.  It is kept because a writer being refused while the scan runs is
    the property the fix relies on, and a later refactor that moved the scan back out of
    the transaction would fail here.  The discriminating test for this batch is the
    rebuild one above.
    """
    _seed()
    memory_gram_index.rebuild_memory_gram_index()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory WHERE memory_id = 'mem_1'")
    assert memory_gram_index.retired_doc_count() == 1

    # ``_entries`` is called once per base blob while prune rewrites them, so it is the
    # point at which the scan is under way.  It is only reached when at least one
    # document is retired, which is why the delete above is part of the setup.
    result = _run_holding_lock_at(
        monkeypatch, "_entries", memory_gram_index.prune_retired_gram_docs
    )

    assert result["retired"] == 1
    assert memory_gram_index.pending_doc_count() == 0
