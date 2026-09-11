import pytest

from vector_lake import governance_store


def _record(memory_id, text, score):
    return {
        "memory_id": memory_id,
        "memory_type": "fact",
        "memory_key": memory_id,
        "text": text,
        "source_page": "Concept_Query-Relevance.md",
        "validity_state": "active",
        "memory_score": score,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def test_literals_beat_generic_cjk_overlap_and_persisted_score(
    isolated_memory, monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "0")
    records = [
        _record("topical", "Cursor 与 CMS 都保留为发布资格比较候选。", 0.1),
        _record("generic", "请比较发布资格依据与当前问题。", 1.0),
    ]
    governance_store.initialize_meta_store()
    governance_store.save_memory_objects(
        {"items": {item["memory_id"]: item for item in records}}
    )

    rows = governance_store.search_operational_memory(
        "请比较 Cursor 与 CMS 的发布资格依据", top_k=2,
    )

    assert [row["memory_id"] for row in rows] == ["topical", "generic"]
    assert all(0.0 <= row["retrieval_score"] <= 1.0 for row in rows)


def test_cjk_single_character_fallback_and_stable_id_tie(
    isolated_memory, monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "0")
    records = [
        _record("memory-z", "甲型路径", 0.8),
        _record("memory-a", "甲型路径", 0.8),
        _record("memory-aa", "甲型路径", 0.8),
    ]
    governance_store.initialize_meta_store()
    governance_store.save_memory_objects(
        {"items": {item["memory_id"]: item for item in records}}
    )

    rows = governance_store.search_operational_memory("甲", top_k=3)

    assert [row["memory_id"] for row in rows] == ["memory-a", "memory-aa", "memory-z"]
    assert [row["retrieval_score"] for row in rows] == [1.0, 1.0, 1.0]


@pytest.mark.parametrize("indexed", [False, True])
def test_short_long_and_comparison_queries_share_bounded_ranking(isolated_memory, monkeypatch, indexed):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1" if indexed else "0")
    governance_store.initialize_meta_store()
    records = [
        _record("alabama", "CMS Alabama 144 million rural healthcare transformation", 0.1),
        _record("virginia", "CMS Virginia 122 million rural healthcare transformation", 0.2),
        _record("generic", "请问数字化建设的来源支持与能力判断是什么。", 1.0),
    ]
    governance_store.save_memory_objects({"items": {item["memory_id"]: item for item in records}})
    if indexed:
        governance_store.maintain_operational_memory_search_index(batch_size=100)
    for query in ("CMS Alabama 144 million", "请问 CMS Alabama 144 million 的数字化建设有哪些来源支持？"):
        current = governance_store.search_operational_memory(query, top_k=3)
        reference, _ = governance_store._legacy_operational_memory_views(query, 3, 3, None, False)
        assert current[0]["memory_id"] == "alabama"
        assert [(x["memory_id"], x["retrieval_score"]) for x in current] == [(x["memory_id"], x["retrieval_score"]) for x in reference]
    compared = governance_store.search_operational_memory("CMS Alabama versus Virginia", top_k=3)
    assert {x["memory_id"] for x in compared[:2]} == {"alabama", "virginia"}


def test_sqlite_context_preserves_explicit_structural_alias(monkeypatch):
    import json
    from vector_lake import db_store, tool_search
    from tests.test_readable_snippets import _plaintext_db

    conn = _plaintext_db()
    conn.execute("CREATE TABLE canonical_identities (record_kind TEXT, record_id TEXT, page_key TEXT, data_json TEXT)")
    records = [
        ("nav", "Concept_Orphan-Index", {"title": "Concept Orphan Index", "aliases": ["Friendly Navigator"], "raw_text": "Cursor navigation and related pages.", "status": "active"}),
        ("report", "System_LegitimateReport", {"title": "System Report", "raw_text": "Cursor deployment evidence is discussed.", "status": "active"}),
    ]
    for entity_id, key, record in records:
        conn.execute("INSERT INTO entities VALUES (?, ?, ?)", (entity_id, record["title"], json.dumps(record)))
        conn.execute("INSERT INTO entity_identities VALUES (?, ?, '{}')", (entity_id, key))
    monkeypatch.setattr(db_store, "get_connection", lambda: conn)
    monkeypatch.setattr(db_store, "search_wiki", lambda *_args, **_kwargs: [{"node_key": key, "rank": -1.0} for _, key, _ in records])
    signature = ("gen", 1, "digest", "canonical", 1)
    monkeypatch.setattr(db_store, "verify_search_projection_integrity", lambda *_: {"status": "ready", "signature": signature})
    monkeypatch.setattr(tool_search, "_search_projection_generation_issue", lambda *_: None)
    monkeypatch.setattr(tool_search, "_context_purpose", lambda *_: "purpose")
    monkeypatch.setattr(tool_search, "build_memory_packet", lambda *_args, **_kwargs: {"packet": "", "memory_count": 0, "warning_count": 0, "omitted_count": 0})
    ordinary = tool_search._assemble_sqlite_context("Cursor", 10000)
    assert "Concept_Orphan-Index" not in ordinary["wiki_context"]
    assert "System_LegitimateReport" in ordinary["wiki_context"]
    explicit = tool_search._assemble_sqlite_context("Friendly Navigator", 10000)
    assert "Concept_Orphan-Index" in explicit["wiki_context"]
    conn.execute("DELETE FROM entities WHERE entity_id='nav'")
    deleted = tool_search._assemble_sqlite_context("Friendly Navigator", 10000)
    assert "Concept_Orphan-Index" not in deleted["wiki_context"]
