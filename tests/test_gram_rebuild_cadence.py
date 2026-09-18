"""The gram index has a rebuild cadence, and it belongs to maintenance, not to a read.

The indexed path saves ~0.355 s per search on the live corpus, while one rebuild refuses
every writer for ~430 s.  So the index is allowed to fall behind and is settled later, at a
point where that write lock is acceptable.  These tests pin the three halves of that
contract:

* the threshold decides due, in documents written rather than in calls;
* the maintenance entry point rebuilds at the threshold and does nothing below it;
* the read path still refuses to rebuild a *stale* base at any size -- the rule the cadence
  depends on, and the one a later "just self-heal it" patch would break.
"""

from tests.test_gram_index_exactness import _put

from vector_lake import db_store, memory_gram_index


def _write(count: int, start: int = 0) -> None:
    db_store.init_db()
    conn = db_store.get_connection()
    for index in range(start, start + count):
        _put(conn, f"mem_{index}", f"医院 电子病历 六级 目标 {index}")


def _build() -> None:
    memory_gram_index.rebuild_memory_gram_index()


def test_a_fresh_index_is_not_due(isolated_memory, monkeypatch):
    monkeypatch.setattr(memory_gram_index, "REBUILD_AFTER_WRITES", 3)
    _write(3)
    _build()

    assert memory_gram_index.writes_since_rebuild() == 0
    assert memory_gram_index.rebuild_due() is False


def test_the_threshold_counts_documents_not_calls(isolated_memory, monkeypatch):
    monkeypatch.setattr(memory_gram_index, "REBUILD_AFTER_WRITES", 3)
    _write(3)
    _build()

    _write(1, start=10)
    _write(1, start=11)
    assert memory_gram_index.writes_since_rebuild() == 2
    assert memory_gram_index.rebuild_due() is False

    _write(1, start=12)
    assert memory_gram_index.writes_since_rebuild() == 3
    assert memory_gram_index.rebuild_due() is True


def test_editing_one_document_twice_counts_once(isolated_memory, monkeypatch):
    """Staleness is per document: the second write adds no further scan cost."""
    monkeypatch.setattr(memory_gram_index, "REBUILD_AFTER_WRITES", 2)
    _write(2)
    _build()

    conn = db_store.get_connection()
    _put(conn, "mem_0", "医院 电子病历 六级 目标 0 改写")
    _put(conn, "mem_0", "医院 电子病历 六级 目标 0 再改写")

    assert memory_gram_index.writes_since_rebuild() == 1
    assert memory_gram_index.rebuild_due() is False


def test_the_maintenance_entry_point_does_nothing_below_the_threshold(isolated_memory, monkeypatch):
    monkeypatch.setattr(memory_gram_index, "REBUILD_AFTER_WRITES", 3)
    _write(3)
    _build()
    _write(2, start=10)

    calls: list[bool] = []
    real = memory_gram_index.rebuild_memory_gram_index

    def spy(dry_run=False, batch_docs=2000):
        calls.append(dry_run)
        return real(dry_run=dry_run, batch_docs=batch_docs)

    monkeypatch.setattr(memory_gram_index, "rebuild_memory_gram_index", spy)
    notice = memory_gram_index.maybe_rebuild_memory_gram_index()

    assert calls == [], "a rebuild ran although the threshold was not reached"
    assert "not due" in notice
    assert "2 document(s)" in notice


def test_the_maintenance_entry_point_settles_the_debt_at_the_threshold(isolated_memory, monkeypatch):
    monkeypatch.setattr(memory_gram_index, "REBUILD_AFTER_WRITES", 3)
    _write(3)
    _build()
    _write(3, start=10)

    # The write made the index unusable, which is what the cadence exists to undo.
    assert memory_gram_index.gram_index_usable() is False
    assert "is due" in memory_gram_index.maybe_rebuild_memory_gram_index(dry_run=True)

    report = memory_gram_index.maybe_rebuild_memory_gram_index()

    assert "Rebuilt the memory gram index" in report
    assert memory_gram_index.gram_index_usable() is True
    assert memory_gram_index.writes_since_rebuild() == 0


def test_the_read_path_still_refuses_to_rebuild_a_stale_base(isolated_memory, monkeypatch):
    """The rule the cadence rests on: nothing on the read path settles this debt.

    A rebuild inside a search is the wrong trade at any size -- it holds the write lock for
    the length of the build -- so ``ensure_memory_gram_index`` answers False and the caller
    scans.  If a later change makes the read path self-heal, this fails and the cadence's
    threshold stops meaning anything.
    """
    monkeypatch.setattr(memory_gram_index, "REBUILD_AFTER_WRITES", 1)
    _write(2)
    _build()
    _write(5, start=10)

    calls: list[bool] = []
    real = memory_gram_index.rebuild_memory_gram_index

    def spy(dry_run=False, batch_docs=2000):
        calls.append(dry_run)
        return real(dry_run=dry_run, batch_docs=batch_docs)

    monkeypatch.setattr(memory_gram_index, "rebuild_memory_gram_index", spy)

    assert memory_gram_index.rebuild_due() is True
    assert memory_gram_index.ensure_memory_gram_index() is False
    assert calls == [], "the read path rebuilt a stale base"


def test_an_absent_base_is_due_whatever_the_write_count(isolated_memory, monkeypatch):
    """A base that cannot answer is due even with nothing queued.

    ``ready=False`` with ``live=0`` is exactly what a format version bump or a restore
    leaves behind, and on a corpus larger than ``AUTO_REBUILD_MAX_DOCS`` the read path
    refuses to build it.  A write-count-only threshold answered "not due" there forever.
    """
    monkeypatch.setattr(memory_gram_index, "REBUILD_AFTER_WRITES", 500)
    _write(2)

    assert memory_gram_index.writes_since_rebuild() == 2
    assert memory_gram_index.rebuild_due() is True
    assert "no usable base" in memory_gram_index.rebuild_due_reason()

    _build()
    assert memory_gram_index.rebuild_due() is False, "a built base with 0 writes is not due"

    _write(2, start=10)
    assert memory_gram_index.writes_since_rebuild() == 2
    assert memory_gram_index.rebuild_due() is False, "2 < 500"


def test_an_older_format_version_is_due(isolated_memory, monkeypatch):
    _write(2)
    _build()
    assert memory_gram_index.rebuild_due() is False

    monkeypatch.setattr(
        memory_gram_index, "GRAM_FORMAT_VERSION", memory_gram_index.GRAM_FORMAT_VERSION + 1
    )

    assert memory_gram_index.gram_index_usable() is False
    assert memory_gram_index.rebuild_due() is True
    assert "format version" in memory_gram_index.rebuild_due_reason()


def test_overlay_rows_are_due(isolated_memory, monkeypatch):
    monkeypatch.setattr(memory_gram_index, "REBUILD_AFTER_WRITES", 500)
    _write(2)
    _build()

    conn = db_store.get_connection()
    doc = conn.execute("SELECT rowid FROM operational_memory_index LIMIT 1").fetchone()[0]
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO operational_memory_gram_overlay (gram, doc, mask) "
            "VALUES (?, ?, ?)",
            ("医院", doc, 1),
        )

    assert memory_gram_index.writes_since_rebuild() == 0
    assert memory_gram_index.rebuild_due() is True
    assert "overlay row" in memory_gram_index.rebuild_due_reason()


def test_the_threshold_is_not_a_read_path_cap(isolated_memory, monkeypatch):
    """A corpus far larger than the read-path cap is still rebuilt from maintenance.

    ``AUTO_REBUILD_MAX_DOCS`` bounds what a *search* may build, because a search must not
    hold the write lock for minutes.  Maintenance has no such bound, and inheriting the cap
    would mean a large corpus could never be rebuilt at all -- which is the state the live
    lake is in.
    """
    monkeypatch.setattr(memory_gram_index, "REBUILD_AFTER_WRITES", 1)
    monkeypatch.setattr(memory_gram_index, "AUTO_REBUILD_MAX_DOCS", 1)
    _write(3)
    _build()
    _write(1, start=10)

    assert memory_gram_index._projection_doc_count(db_store.get_connection()) > 1
    assert "Rebuilt the memory gram index" in memory_gram_index.maybe_rebuild_memory_gram_index()
    assert memory_gram_index.gram_index_usable() is True
