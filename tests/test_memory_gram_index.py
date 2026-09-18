"""Differential and maintenance tests for the exact n-gram memory index.

The index is only allowed to be a *performance* change, so the central assertion
is that its result is identical to the ``legacy`` full-table scan -- including
the order of tie groups, which is where a naive rewrite silently drifts.
"""

import json

import pytest

from vector_lake import db_store, governance_store, memory_gram_index, tool_doctor


def _put(conn, memory_id: str, text: str, memory_type: str = "fact", score: float = 0.6,
         state: str = "active", page: str | None = None) -> None:
    payload = {
        "memory_id": memory_id,
        "memory_type": memory_type,
        "memory_key": memory_id,
        "text": text,
        "source_page": page if page is not None else f"Concept_{memory_id}.md",
        "validity_state": state,
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


SEED = [
    ("mem_a", "信创 HIS 集成平台 选型结论", "decision", 0.9),
    ("mem_b", "医院 电子病历六级 评级 目标", "task_state", 0.7),
    ("mem_c", "preferred deployment target is Kubernetes", "preference", 0.8),
    ("mem_d", "DRG 医保支付 合规 运营", "fact", 0.6),
    ("mem_e", "DRG 结算 清单 质控", "fact", 0.6),
    ("mem_f", "DRG 病案 首页 上传", "fact", 0.6),
    ("mem_g", "信创 HIS 旧结论", "decision", 0.5),
    ("mem_h", "互联互通 五乙 智慧服务 分级 评估", "fact", 0.55),
    ("mem_i", "unrelated payload", "fact", 0.4),
    ("mem_j", "公立医院 高质量发展 评价 指标", "fact", 0.65),
]


@pytest.fixture
def seeded(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    for memory_id, text, memory_type, score in SEED:
        _put(conn, memory_id, text, memory_type, score)
    assert memory_gram_index.ensure_memory_gram_index() is True
    return isolated_memory


def _ids(backend: str, query: str, **kwargs):
    import os

    os.environ["VECTOR_LAKE_MEMORY_SEARCH"] = backend
    return [m["memory_id"] for m in governance_store.search_operational_memory(query, **kwargs)]


@pytest.mark.parametrize(
    "query,kwargs",
    [
        ("信创 医院 HIS 集成平台", {"top_k": 8}),
        ("DRG 医保支付", {"top_k": 3}),
        ("DRG", {"top_k": 8}),
        ("deployment target", {"top_k": 5}),
        ("电子病历六级", {"top_k": 5}),
        ("互联互通 五乙", {"top_k": 5}),
        ("公立医院高质量发展", {"top_k": 5}),
        ("unrelated", {"top_k": 5}),
        ("", {"top_k": 6}),
        ("信创", {"top_k": 8, "include_history": True}),
        ("HIS", {"top_k": 8, "memory_types": ["decision"]}),
        ("DRG", {"top_k": 8, "memory_types": ["fact"]}),
        ("信创", {"top_k": 8, "memory_types": ["preference"], "include_history": True}),
        ("医院", {"top_k": 4}),
    ],
)
def test_gram_backend_is_identical_to_the_full_scan(seeded, query, kwargs):
    assert _ids("gram", query, **kwargs) == _ids("legacy", query, **kwargs)


def test_gram_relevance_matches_the_sql_scorer(seeded):
    conn = db_store.get_connection()
    for query in ("信创 医院", "DRG", "互联互通 五乙 智慧服务", "x", "HIS 集成平台"):
        terms = governance_store._query_terms(query)
        indexed = memory_gram_index.accumulate_relevance(
            terms, skip_docs=memory_gram_index.skip_doc_set(conn)
        )
        reference: dict[int, int] = {}
        for row in conn.execute(
            "SELECT rowid, key_blob, text_blob, page_blob, memory_type FROM operational_memory_index"
        ):
            relevance = sum(
                4 * (term in row["key_blob"])
                + 3 * (term in row["text_blob"])
                + (term in row["page_blob"])
                + (term in row["memory_type"])
                for term in terms
            )
            if relevance:
                reference[int(row["rowid"])] = relevance
        assert indexed == reference, query


def test_a_write_defers_to_the_exact_scan(seeded):
    conn = db_store.get_connection()
    _put(conn, "mem_a", "医院 电子病历六级 目标")

    assert memory_gram_index.pending_doc_count() == 1
    assert _ids("gram", "电子病历 医院", top_k=5) == _ids("legacy", "电子病历 医院", top_k=5)
    # The answer is the exact one, and it came from the full scan: a stale base is
    # not rebuilt behind the read's back, however small the corpus, because the scan
    # is far cheaper than a rebuild.  Only an explicit rebuild clears the queue.
    assert memory_gram_index.pending_doc_count() == 1
    assert memory_gram_index.gram_index_usable() is False

    memory_gram_index.rebuild_memory_gram_index()

    assert memory_gram_index.pending_doc_count() == 0
    assert _ids("gram", "信创 集成平台", top_k=5) == _ids("legacy", "信创 集成平台", top_k=5)


def test_a_deleted_document_is_exact_without_a_rebuild(seeded):
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory WHERE memory_id = 'mem_d'")

    assert memory_gram_index.retired_doc_count() == 1
    # The counts are what witness the skip; the equality below is an end-to-end smoke
    # that cannot fail from a missing one, because the deleted document's rowid is
    # dropped by the projection join and the result window is wider than the corpus.
    assert _ids("gram", "DRG 医保支付 合规", top_k=6) == _ids("legacy", "DRG 医保支付 合规", top_k=6)
    assert memory_gram_index.pending_doc_count() == 1

    assert memory_gram_index.rebuild_memory_gram_index().startswith("Rebuilt")
    assert memory_gram_index.pending_doc_count() == 0
    assert _ids("gram", "DRG", top_k=6) == _ids("legacy", "DRG", top_k=6)


def test_inserted_documents_join_the_index(seeded):
    conn = db_store.get_connection()
    _put(conn, "mem_new", "跨机构 影像 云 平台 选型")

    assert _ids("gram", "影像 云 平台", top_k=4) == _ids("legacy", "影像 云 平台", top_k=4)


def test_rebuild_reports_and_restores_the_base(seeded):
    dry = memory_gram_index.rebuild_memory_gram_index(dry_run=True)
    assert "Would rebuild" in dry

    with db_store.transaction():
        db_store.get_connection().execute("DELETE FROM operational_memory_gram")

    assert memory_gram_index.gram_index_state()["ready"] is False
    assert memory_gram_index.gram_index_usable() is False
    # A short corpus is rebuilt automatically on first use.
    assert memory_gram_index.ensure_memory_gram_index() is True
    assert _ids("gram", "信创 医院", top_k=5) == _ids("legacy", "信创 医院", top_k=5)


def test_backend_falls_back_when_the_index_tables_are_missing(seeded):
    with db_store.transaction():
        db_store.get_connection().execute("DROP TABLE operational_memory_gram")

    assert _ids("gram", "DRG", top_k=4) == _ids("legacy", "DRG", top_k=4)


def test_composite_terms_need_real_adjacency(seeded):
    conn = db_store.get_connection()
    # Every bigram of "医院电子" exists somewhere, but no document contains the run,
    # so the composite term must contribute nothing.  The query as a whole still
    # matches on the single-character terms, and must match the oracle exactly.
    terms = governance_store._query_terms("医院电子")
    indexed = memory_gram_index.accumulate_relevance(
        terms, skip_docs=memory_gram_index.skip_doc_set(conn)
    )
    composite_only = memory_gram_index.accumulate_relevance(
        ["医院电子"], skip_docs=memory_gram_index.skip_doc_set(conn)
    )

    assert composite_only == {}
    assert indexed
    assert _ids("gram", "医院电子", top_k=5) == _ids("legacy", "医院电子", top_k=5)


def test_index_report_is_operator_readable(seeded, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_SEARCH", "gram")
    report = memory_gram_index.memory_gram_index_report()

    assert "ready=True" in report
    assert "usable=True" in report
    assert "backend=gram" in report


def test_doctor_reports_a_usable_gram_index(seeded):
    report = tool_doctor.doctor_vector_lake()

    assert "[OK] Memory Gram Index:" in report
    assert "usable=True" in report
    assert "memory_gram_index_unusable" not in report


def test_doctor_flags_an_unusable_gram_index(seeded):
    conn = db_store.get_connection()
    for index in range(memory_gram_index.AUTO_REBUILD_MAX_DOCS + 1):
        _put(conn, f"bulk_{index}", f"bulk payload {index}")
    assert memory_gram_index.live_dirty_doc_count(conn) > memory_gram_index.AUTO_REBUILD_MAX_DOCS

    report = tool_doctor.doctor_vector_lake()

    assert "[OK] Memory Gram Index:" in report
    assert "usable=False" in report
    assert "memory_gram_index_unusable" in report


def test_the_read_path_does_not_drain_a_backlog_beyond_the_cap(seeded):
    """Past the cap a read-path rebuild cannot reach usability, so it is not attempted.

    The read path only builds an *absent* base, and only while the corpus fits in
    ``AUTO_REBUILD_MAX_DOCS``; a pending backlog of documents with stale base postings is
    never built or drained from a search.  A rebuild is the documented way out.
    """
    conn = db_store.get_connection()
    for index in range(memory_gram_index.AUTO_REBUILD_MAX_DOCS + 1):
        _put(conn, f"bulk_{index}", f"bulk payload {index}")
    backlog = memory_gram_index.live_dirty_doc_count(conn)
    assert backlog > memory_gram_index.AUTO_REBUILD_MAX_DOCS

    assert memory_gram_index.ensure_memory_gram_index() is False

    # Nothing was built and the queue is untouched: a search settles no debt.
    assert memory_gram_index.live_dirty_doc_count(conn) == backlog


def test_the_queue_breakdown_is_one_consistent_snapshot(seeded):
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory WHERE memory_id = 'mem_a'")
    _put(conn, "mem_new", "跨机构 影像 云 平台 选型")

    total, live, retired = memory_gram_index.dirty_breakdown(conn)

    assert (total, live, retired) == (2, 1, 1)
    assert total == memory_gram_index.pending_doc_count(conn)
    assert live == memory_gram_index.live_dirty_doc_count(conn)
    assert retired == memory_gram_index.retired_doc_count(conn)
    assert retired >= 0


def test_doctor_treats_a_missing_gram_index_as_degradation(isolated_memory):
    """A database written before the index existed must not report a doctor FAIL."""
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        for table in (
            "operational_memory_gram",
            "operational_memory_gram_dirty",
            "operational_memory_gram_state",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")

    report = tool_doctor.doctor_vector_lake()

    assert "[FAIL] Memory Gram Index" not in report
    assert "[OK] Memory Gram Index:" in report
    assert "memory_gram_index_unusable" in report
