"""The index is only allowed to answer when its answer is the exact one.

``tests/test_memory_gram_index.py`` checks the index against the full-table scan
while the base is current.  This file covers the states in between: what the read
path must refuse to serve, and why draining the queue is not a route out of them.
"""

import json

import pytest

from vector_lake import db_store, governance_store, memory_gram_index


def _put(conn, memory_id: str, text: str, memory_type: str = "fact", score: float = 0.6) -> None:
    payload = {
        "memory_id": memory_id,
        "memory_type": memory_type,
        "memory_key": memory_id,
        "text": text,
        "source_page": f"Concept_{memory_id}.md",
        "validity_state": "active",
        "memory_score": score,
        "updated_at": "2026-07-14T00:00:00+00:00",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO operational_memory "
            "(memory_id, memory_type, score, status, ttl, data_json, updated_at) VALUES (?,?,?,?,?,?,?)",
            (memory_id, memory_type, score, "Active", 365.0,
             json.dumps(payload, ensure_ascii=False), payload["updated_at"]),
        )


def _ids(backend: str, query: str, **kwargs):
    import os

    os.environ["VECTOR_LAKE_MEMORY_SEARCH"] = backend
    return [m["memory_id"] for m in governance_store.search_operational_memory(query, **kwargs)]


@pytest.fixture
def one_doc(isolated_memory):
    """A corpus whose base is a rebuild snapshot of a single document."""
    db_store.init_db()
    conn = db_store.get_connection()
    _put(conn, "mem_x", "医保 支付 合规")
    assert memory_gram_index.rebuild_memory_gram_index().startswith("Rebuilt")
    assert memory_gram_index.gram_index_usable() is True
    return isolated_memory


def test_a_document_is_not_credited_for_a_gram_it_dropped(one_doc):
    """The measured defect, at the level the read path consumes.

    A document is edited to drop a two-character term entirely, then materialised
    and merged exactly the way the maintenance commands do it.  The base still holds
    a posting for the dropped term -- ``_compact_gram_chunk`` rewrites only the grams
    the overlay holds, and a dropped gram is not one of them -- so the term must not
    reach the accumulator unless the queue is still suppressing that document.
    """
    conn = db_store.get_connection()
    _put(conn, "mem_x", "影像 云平台 部署")

    memory_gram_index.flush_memory_gram_dirty()
    memory_gram_index.compact_memory_gram_overlay(limit_grams=None)
    assert memory_gram_index.overlay_row_count() == 0

    relevance = memory_gram_index.accumulate_relevance(
        ["医保"], skip_docs=memory_gram_index.skip_doc_set(conn)
    )

    assert relevance == {}, f"the dropped gram still credits the document: {relevance}"


def test_the_search_agrees_with_the_full_scan_after_a_dropped_gram(one_doc):
    conn = db_store.get_connection()
    _put(conn, "mem_x", "影像 云平台 部署")
    memory_gram_index.flush_memory_gram_dirty()
    memory_gram_index.compact_memory_gram_overlay(limit_grams=None)

    assert _ids("gram", "医保", top_k=5) == _ids("legacy", "医保", top_k=5)
    assert _ids("gram", "影像 云平台", top_k=5) == _ids("legacy", "影像 云平台", top_k=5)


def test_materialising_a_document_does_not_retire_its_queue_entry(one_doc):
    _put(db_store.get_connection(), "mem_x", "影像 云平台 部署")

    memory_gram_index.flush_memory_gram_dirty()

    assert memory_gram_index.live_dirty_doc_count() == 1
    assert memory_gram_index.gram_index_usable() is False


def test_compaction_does_not_make_the_base_authoritative(one_doc):
    """Merging the overlay must not look like a return to exactness."""
    _put(db_store.get_connection(), "mem_x", "影像 云平台 部署")
    memory_gram_index.flush_memory_gram_dirty()

    memory_gram_index.compact_memory_gram_overlay(limit_grams=None)

    assert memory_gram_index.overlay_row_count() == 0
    assert memory_gram_index.live_dirty_doc_count() == 1
    assert memory_gram_index.gram_index_usable() is False


def test_retired_markers_alone_still_allow_the_fast_path(one_doc):
    """A deleted document is removed by being queued, so it cannot block serving."""
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory WHERE memory_id = 'mem_x'")

    assert memory_gram_index.live_dirty_doc_count() == 0
    assert memory_gram_index.retired_doc_count() == 1
    assert memory_gram_index.gram_index_usable() is True
    assert _ids("gram", "医保", top_k=5) == _ids("legacy", "医保", top_k=5) == []


def test_the_read_path_does_not_drain_the_queue(one_doc, monkeypatch):
    """The sequence that produced the wrong answer is not reachable from a read.

    The calls are recorded rather than expected to raise: ``ensure_memory_gram_index``
    catches every exception and degrades to the exact scan, which is also the oracle
    the comparison below uses, so a raising stub would let this test pass while the
    read path was in fact draining.
    """
    _put(db_store.get_connection(), "mem_x", "影像 云平台 部署")
    calls: list[str] = []

    def _record(name):
        def _stub(*args, **kwargs):
            calls.append(name)
            return {}

        return _stub

    monkeypatch.setattr(memory_gram_index, "flush_memory_gram_dirty", _record("flush"))
    monkeypatch.setattr(memory_gram_index, "compact_memory_gram_overlay", _record("compact"))

    assert _ids("gram", "影像 云平台", top_k=5) == _ids("legacy", "影像 云平台", top_k=5)
    assert calls == []


def test_an_absent_base_is_still_built_on_first_use(one_doc):
    """The one case where a read pays for a rebuild: there is nothing to serve."""
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory_gram")

    assert memory_gram_index.gram_index_state()["ready"] is False
    assert memory_gram_index.ensure_memory_gram_index() is True
    assert _ids("gram", "医保", top_k=5) == _ids("legacy", "医保", top_k=5)


def test_a_base_from_the_previous_release_is_not_trusted(one_doc):
    """A version-1 base can satisfy the serving predicate over a stale posting.

    The release that shipped version 1 drained the queue and merged the overlay, which
    leaves no live marker and no overlay row over a base that both misses a
    document's current grams and still posts the ones it dropped.  No counter tells
    that state apart from a clean one, so the format version has to.
    """
    conn = db_store.get_connection()
    _put(conn, "mem_x", "影像 云平台 部署")
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory_gram_dirty WHERE doc IN "
                     "(SELECT rowid FROM operational_memory_index)")
        conn.execute("UPDATE operational_memory_gram_state SET format_version = 1")

    assert memory_gram_index.live_dirty_doc_count() == 0
    assert memory_gram_index.overlay_row_count() == 0
    assert memory_gram_index.gram_index_usable() is False

    assert memory_gram_index.ensure_memory_gram_index() is True
    assert memory_gram_index.gram_index_state()["format_version"] == memory_gram_index.GRAM_FORMAT_VERSION
    assert memory_gram_index.accumulate_relevance(
        ["医保"], skip_docs=memory_gram_index.skip_doc_set(conn)
    ) == {}
